"""Plan 07-03: the candidate's decision on the API login, and the worker's release that runs both keys again (INDEX
decision 4). The API never inserts an action or a check; the worker does."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import jobs  # noqa: F401  (registers the handlers)
from app.platform import db, queue
from app.platform.errors import ApiError
from tests.guardian.support import (
    approve_and_release,
    entails,
    follow_up_payload,
    make_committed_world,
    propose_follow_up,
    rows,
    scalar,
)


def hash_of(payload=None) -> str:
    return guardian.payload_hash(payload or follow_up_payload())


def decide(world, approval_id, **kwargs):
    kwargs.setdefault("payload_hash", hash_of())
    with db.transaction(world.user) as conn:
        return guardian.decide(conn, world.user, approval_id, **kwargs)


@pytest.fixture
def proposed(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    return world, propose_follow_up(world)


def test_approving_records_the_yes_and_enqueues_the_release_but_schedules_nothing(engine, proposed):
    world, p = proposed
    decision = decide(world, p.approval_id, approve=True)
    assert (decision.approval_status, decision.action_status, decision.release) == (
        "approved",
        "awaiting_approval",
        "pending",
    )
    row = rows(
        engine, "SELECT status::text, decided_via, decided_at IS NOT NULL FROM approvals WHERE id = :a", a=p.approval_id
    )[0]
    assert tuple(row) == ("approved", "app", True)
    job = rows(
        engine,
        "SELECT queue, payload ->> 'approval_id', payload -> 'edits' FROM queue_jobs WHERE kind = 'guardian.release'",
    )
    assert [(j[0], j[1], j[2]) for j in job] == [("interactive", str(p.approval_id), None)]
    assert scalar(engine, "SELECT count(*) FROM outbox") == 0
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "awaiting_approval"


def test_the_release_schedules_the_follow_up_with_its_undo_window_and_one_outbox_row(engine, proposed):
    world, p = proposed
    with engine.begin() as c:
        c.execute(text("UPDATE users SET undo_window_minutes = 25 WHERE id = :u"), {"u": world.user})
    assert approve_and_release(world, p) == "scheduled"
    action = rows(
        engine,
        "SELECT status::text, extract(epoch FROM undo_until - clock_timestamp()) / 60 FROM agent_actions WHERE id = :i",
        i=p.action_id,
    )[0]
    assert action[0] == "scheduled" and 24.5 < float(action[1]) <= 25
    message = rows(
        engine,
        "SELECT template, dedupe_key, payload, user_id, "
        "not_before = (SELECT undo_until FROM agent_actions WHERE id = :i) "
        "FROM outbox WHERE agent_action_id = :i",
        i=p.action_id,
    )
    assert len(message) == 1
    assert tuple(message[0]) == (
        "follow_up_v1",
        f"action:{p.action_id}",
        {"agent_action_id": p.action_id},
        world.user,
        True,
    )
    # a second run of the same release changes nothing
    assert guardian.release(p.approval_id, None) == "noop"
    assert scalar(engine, "SELECT count(*) FROM outbox") == 1
    # the release reran both keys with new check rows
    assert scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=p.action_id) == 12


def test_a_packet_is_scheduled_with_no_outbox_row(engine, clean, monkeypatch, tmp_path):
    from tests.guardian.support import packet_payload

    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    payload = packet_payload(world.job_id)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "operator", "test-1")
        p = guardian.propose_action(
            conn,
            run,
            action_type="submit_application",
            payload=payload,
            provenance={"fields": {"answers.why": "candidate"}},
        )
    assert p.status == "awaiting_approval"
    assert approve_and_release(world, p, payload) == "scheduled"
    row = rows(engine, "SELECT status::text, undo_until FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(row) == ("scheduled", None)
    assert scalar(engine, "SELECT count(*) FROM outbox") == 0


def test_a_stale_hash_a_second_decision_a_paused_campaign_and_a_bad_edit_are_refused(engine, proposed):
    world, p = proposed
    with pytest.raises(ApiError) as stale:
        decide(world, p.approval_id, approve=True, payload_hash="0" * 64)
    assert (stale.value.status, stale.value.code) == (409, "preview_changed")
    with engine.begin() as c:
        c.execute(text("UPDATE campaigns SET paused_at = now()"))
    with pytest.raises(ApiError) as paused:
        decide(world, p.approval_id, approve=True)
    assert (paused.value.status, paused.value.code, paused.value.details) == (409, "conflict", {"state": "paused"})
    with engine.begin() as c:
        c.execute(text("UPDATE campaigns SET paused_at = NULL"))
    with pytest.raises(ApiError) as bad:
        decide(world, p.approval_id, approve=True, edits={"to": "other@lumen.example"})
    assert (bad.value.status, bad.value.code) == (422, "validation_failed")
    assert scalar(engine, "SELECT status::text FROM approvals WHERE id = :a", a=p.approval_id) == "pending"
    decide(world, p.approval_id, approve=True)
    with pytest.raises(ApiError) as twice:
        decide(world, p.approval_id, approve=True)
    assert (twice.value.status, twice.value.code) == (409, "already_resolved")


def test_a_closed_campaign_refuses_approval(engine, proposed):
    world, p = proposed
    with engine.begin() as c:
        c.execute(text("UPDATE campaigns SET stage = 'closed', closed_at = now(), closed_reason = 'cancelled'"))
    with pytest.raises(ApiError) as closed:
        decide(world, p.approval_id, approve=True)
    assert closed.value.details == {"state": "closed"}


def test_someone_elses_approval_is_not_found(engine, proposed):
    world, p = proposed
    other = make_committed_world(engine, consent=False)
    with db.transaction(other.user) as conn, pytest.raises(ApiError) as caught:
        guardian.decide(conn, other.user, p.approval_id, approve=True, payload_hash=hash_of())
    assert caught.value.status == 404


def test_reject_marks_approval_and_action_rejected_with_the_note(engine, proposed):
    world, p = proposed
    decision = decide(world, p.approval_id, approve=False, note="not now")
    assert (decision.approval_status, decision.action_status, decision.release) == ("rejected", "rejected", None)
    row = rows(
        engine,
        "SELECT p.status::text, p.note, a.status::text, a.outcome_code "
        "FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id",
    )[0]
    assert tuple(row) == ("rejected", "not now", "rejected", "rejected")
    assert scalar(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'guardian.release'") == 0


def test_approving_with_edits_rejects_the_original_and_the_release_schedules_the_replacement(engine, proposed):
    world, p = proposed
    edits = {"body": "Hello Meera, I wanted to check whether there is any news on my application. Best regards, Ada"}
    decision = decide(world, p.approval_id, approve=True, edits=edits)
    assert (decision.approval_status, decision.note, decision.action_status) == ("rejected", "edited", "rejected")
    job = rows(engine, "SELECT payload -> 'edits' ->> 'body' FROM queue_jobs WHERE kind = 'guardian.release'")
    assert job[0][0] == edits["body"]
    assert guardian.release(p.approval_id, edits) == "scheduled"
    both = rows(
        engine,
        "SELECT id, status::text, payload ->> 'body', inputs ->> 'replaces' "
        "FROM agent_actions WHERE kind = 'outside_action' ORDER BY id",
    )
    assert [b[1] for b in both] == ["rejected", "scheduled"]
    assert both[1][2] == edits["body"] and both[1][3] == str(p.action_id)
    replacement = both[1][0]
    approval = rows(
        engine, "SELECT status::text, decided_via FROM approvals WHERE agent_action_id = :a", a=replacement
    )[0]
    assert tuple(approval) == ("approved", "app")
    fields = scalar(engine, "SELECT inputs -> 'provenance' -> 'fields' FROM agent_actions WHERE id = :a", a=replacement)
    assert fields["body"] == "candidate"
    assert scalar(engine, "SELECT count(*) FROM outbox WHERE agent_action_id = :a", a=replacement) == 1
    # the original's text is untouched, and a second release makes no second replacement
    assert (
        scalar(engine, "SELECT payload ->> 'body' FROM agent_actions WHERE id = :a", a=p.action_id)
        == follow_up_payload()["body"]
    )
    assert guardian.release(p.approval_id, edits) == "noop"
    assert scalar(engine, "SELECT count(*) FROM agent_actions WHERE kind = 'outside_action'") == 2


def test_an_edit_that_breaks_a_check_leaves_a_blocked_replacement_and_nothing_sent(engine, proposed):
    world, p = proposed
    edits = {"body": "Hello, my salary expectation is 90000 and you can email me at ada@mail.example."}
    decide(world, p.approval_id, approve=True, edits=edits)
    assert guardian.release(p.approval_id, edits) == "blocked"
    row = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE inputs ->> 'replaces' IS NOT NULL")[
        0
    ]
    assert tuple(row) == ("blocked", "restricted_data")
    assert scalar(engine, "SELECT count(*) FROM outbox") == 0


def test_the_release_job_handler_runs_the_release(engine, proposed):
    world, p = proposed
    decide(world, p.approval_id, approve=True)
    assert queue.run_once(lambda: db.transaction(role="worker"), "test-worker", ["interactive"]) == 1
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"
