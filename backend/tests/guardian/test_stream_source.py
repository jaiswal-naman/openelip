"""08-02: the Guardian source of the live stream (pending count, recorder.step, approval.status, action.status).
Invented rows from tests/campaigns/factories.py, committed, read through the source on the caller's API connection."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian.stream import ID_SLACK, poll, snapshot
from app.platform import db
from tests.campaigns import factories as f
from tests.guardian.support import World, add_spend, make_committed_world

PAST = datetime.now(timezone.utc) - timedelta(minutes=5)


def start(user):
    with db.transaction(user) as conn:
        return snapshot(conn, user)


def next_poll(user, state):
    with db.transaction(user) as conn:
        return poll(conn, user, state)


def world(engine) -> World:
    return make_committed_world(engine)


def run_of(engine, w, role="writer"):
    with engine.begin() as conn:
        return f.make_run(conn, w.user, w.campaign, role)


def add(engine, run, **kwargs):
    if kwargs.get("kind", "outside_action") != "outside_action":
        kwargs.setdefault("status", "recorded")
    with engine.begin() as conn:
        return f.make_action(conn, run, **kwargs)


def test_snapshot_counts_pending_approvals_and_sends_no_history(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    with engine.begin() as conn:
        a = f.make_action(conn, run)
        f.make_approval(conn, a)
        b = f.make_action(conn, run)
        f.make_approval(conn, b, status="approved")
    part, state = start(w.user)
    assert part == {"pending_approvals": 1}
    assert next_poll(w.user, state)[0] == []  # what existed at the snapshot is not announced again


def test_each_new_recorder_row_is_one_event_with_its_id_and_cost(clean, engine):
    w = world(engine)
    run = run_of(engine, w, "researcher")
    _, state = start(w.user)
    call = add_spend(engine, w, 6_100)
    first = add(engine, run, kind="model_call", summary="Read the careers page.", inputs={"llm_call_ids": [call]})
    second = add(engine, run, kind="tool_call", summary="Looked up the company.")
    events, state = next_poll(w.user, state)
    assert [(e.name, e.id) for e in events] == [("recorder.step", first), ("recorder.step", second)]
    assert events[0].data == {
        "campaign_id": w.campaign,
        "id": first,
        "run_id": run,
        "seq": 1,
        "agent_role": "researcher",
        "kind": "model_call",
        "status": "recorded",
        "summary": "Read the careers page.",
        "cost_usd": "0.006100",
    }
    assert next_poll(w.user, state)[0] == []  # each once


def test_a_row_that_commits_late_with_a_lower_id_is_still_sent_once(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    other = run_of(engine, w, "chaser")
    _, state = start(w.user)
    late = engine.connect()
    transaction = late.begin()  # takes the lower identity but commits after the higher one
    low = f.make_action(late, run, kind="model_call", status="recorded")
    high = add(engine, other, kind="model_call")
    assert low < high
    events, state = next_poll(w.user, state)
    assert [e.id for e in events] == [high]
    transaction.commit()
    late.close()
    events, state = next_poll(w.user, state)
    assert [e.id for e in events] == [low]
    assert next_poll(w.user, state)[0] == []


def test_ids_far_below_the_newest_are_pruned_from_memory(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, status, summary)
                   SELECT :run, :u, :c, 100 + n, 'model_call', 'recorded', 'x' FROM generate_series(1, :n) n"""
            ),
            {"run": run, "u": w.user, "c": w.campaign, "n": ID_SLACK + 50},
        )
    _, state = start(w.user)
    last = add(engine, run, kind="model_call")
    events, state = next_poll(w.user, state)
    assert [e.id for e in events] == [last]
    assert len(state.sent) <= ID_SLACK + 1 and state.floor > 0


def test_an_approval_is_announced_when_created_and_when_decided(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    action = add(engine, run, status="awaiting_approval")
    _, state = start(w.user)
    with engine.begin() as conn:
        approval = f.make_approval(conn, action)
    events, state = next_poll(w.user, state)
    assert [e.name for e in events if e.name == "approval.status"] == ["approval.status"]
    created = next(e for e in events if e.name == "approval.status")
    assert created.data == {
        "approval_id": approval,
        "campaign_id": w.campaign,
        "kind": "submit_application",
        "status": "pending",
    }
    with db.transaction(w.user) as conn:
        payload = conn.execute(text("SELECT payload FROM agent_actions WHERE id = :i"), {"i": action}).scalar_one()
        guardian.decide(conn, w.user, approval, approve=False, payload_hash=guardian.payload_hash(payload))
    events, state = next_poll(w.user, state)
    assert [(e.name, e.data["status"]) for e in events if e.name == "approval.status"] == [
        ("approval.status", "rejected")
    ]
    assert next_poll(w.user, state)[0] == []


def test_an_approval_pending_before_the_snapshot_is_followed_after_it(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    action = add(engine, run, status="proposed")
    with engine.begin() as conn:
        approval = f.make_approval(conn, action)
        conn.execute(text("ALTER TABLE approvals DISABLE TRIGGER USER"))  # the test back-dates the request
        conn.execute(
            text("UPDATE approvals SET requested_at = now() - interval '2 hours' WHERE id = :i"), {"i": approval}
        )
        conn.execute(text("ALTER TABLE approvals ENABLE TRIGGER USER"))
    _, state = start(w.user)
    assert next_poll(w.user, state)[0] == []
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approvals SET status = 'approved', decided_at = now(), decided_via = 'app' WHERE id = :i"),
            {"i": approval},
        )
    events, _ = next_poll(w.user, state)
    assert [(e.name, e.data["status"]) for e in events if e.name == "approval.status"] == [
        ("approval.status", "approved")
    ]


def test_an_outside_action_is_followed_to_its_outbox_state_and_reported_once_final(clean, engine):
    w = world(engine)
    run = run_of(engine, w, "chaser")
    _, state = start(w.user)
    action = add(engine, run, action_type="send_follow_up", status="scheduled", undo_until=PAST)
    events, state = next_poll(w.user, state)
    status = [e for e in events if e.name == "action.status"]
    assert [e.data for e in status] == [
        {
            "action_id": action,
            "type": "send_follow_up",
            "status": "scheduled",
            "undo_until": PAST,
            "outbox": None,
            "outcome_code": None,
        }
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO outbox (dedupe_key, user_id, template, payload, not_before, agent_action_id)
                   VALUES ('stream-test-' || :i, :u, 'follow_up', '{}', now(), :i)"""
            ),
            {"i": action, "u": w.user},
        )
    events, state = next_poll(w.user, state)
    assert [e.data["outbox"] for e in events if e.name == "action.status"] == ["pending"]
    assert action in state.open
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE outbox SET status = 'sent', sent_at = now() WHERE agent_action_id = :i"), {"i": action}
        )
        f._move(conn, action, "executed", None)
    events, state = next_poll(w.user, state)
    final = [e.data for e in events if e.name == "action.status"]
    assert [(d["status"], d["outbox"]) for d in final] == [("executed", "sent")]
    assert action not in state.open and action in state.done
    assert next_poll(w.user, state)[0] == []  # a final action is reported once and then dropped


def test_a_blocked_action_carries_its_reason(clean, engine):
    w = world(engine)
    run = run_of(engine, w)
    _, state = start(w.user)
    action = add(engine, run, status="blocked", outcome_code="budget_exhausted")
    events, _ = next_poll(w.user, state)
    (status,) = [e for e in events if e.name == "action.status"]
    assert (status.data["action_id"], status.data["status"], status.data["outcome_code"]) == (
        action,
        "blocked",
        "budget_exhausted",
    )


def test_an_action_still_going_at_the_snapshot_is_followed(clean, engine):
    w = world(engine)
    run = run_of(engine, w, "chaser")
    action = add(engine, run, action_type="send_follow_up", status="scheduled", undo_until=PAST)
    _, state = start(w.user)
    assert next_poll(w.user, state)[0] == []
    with engine.begin() as conn:
        f._move(conn, action, "cancelled", None)
    events, _ = next_poll(w.user, state)
    assert [(e.name, e.data["status"]) for e in events if e.name == "action.status"] == [("action.status", "cancelled")]


def test_another_persons_rows_never_produce_an_event_or_a_count(clean, engine):
    mine, theirs = world(engine), world(engine)
    their_run = run_of(engine, theirs)
    with engine.begin() as conn:
        a = f.make_action(conn, their_run)
        f.make_approval(conn, a)
    part, state = start(mine.user)
    assert part == {"pending_approvals": 0}
    with engine.begin() as conn:
        b = f.make_action(conn, their_run, status="blocked", outcome_code="x")
        f.make_approval(conn, f.make_action(conn, their_run))
    assert b and next_poll(mine.user, state)[0] == []
    assert start(theirs.user)[0] == {"pending_approvals": 2}
