"""Invariant 5 (N3, GRD-02): no outside action runs unless the policy engine and the Guardian both pass it, when
cleared and
again when it runs, and every attempt is logged. Each test fails if a key is skipped at clear, release or send, or if a
blocked attempt leaves no trace."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.guardian import api as guardian
from app.guardian import approvals, jobs, policy  # noqa: F401
from app.platform import db
from app.trust import api as trust
from tests.guardian.support import (
    approve_and_release,
    entails,
    follow_up_payload,
    make_committed_world,
    make_due,
    packet_payload,
    propose_follow_up,
    relay_now,
    rows,
    scalar,
)
from tests.hiring.world import exclude_company


@pytest.fixture
def world(engine, clean, smtp, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path, smtp=smtp)
    return make_committed_world(engine)


def propose_packet(world):
    payload = packet_payload(world.job_id)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "operator", "test-1")
        return guardian.propose_action(
            conn,
            run,
            action_type="submit_application",
            payload=payload,
            provenance={"fields": {"answers.why": "candidate"}},
        )


# ---- at clear: no scheduling without approval, whatever the policy engine says ----


@pytest.mark.parametrize("kind", ["follow_up", "packet"])
def test_a_follow_up_or_packet_never_reaches_scheduled_without_an_approved_approval(engine, world, monkeypatch, kind):
    p = propose_follow_up(world) if kind == "follow_up" else propose_packet(world)
    monkeypatch.setattr(policy, "decide", lambda *a, **k: policy.PolicyResult("allow"))  # key one patched to say yes
    # the worker's release refuses to act on an approval that is still pending
    assert approvals.release(p.approval_id, None) == "noop"
    # and the database refuses the move on its own, however the row is reached
    with pytest.raises(DBAPIError, match="two_key"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE agent_actions SET status = 'scheduled', "
                "undo_until = now() + interval '10 minutes' WHERE id = :i"
            ),
            {"i": p.action_id},
        )
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "awaiting_approval"
    assert scalar(engine, "SELECT count(*) FROM outbox") == 0


def test_a_rejected_or_expired_approval_does_not_release(engine, world):
    p = propose_follow_up(world)
    with db.transaction(world.user) as conn:
        guardian.decide(
            conn, world.user, p.approval_id, approve=False, payload_hash=guardian.payload_hash(follow_up_payload())
        )
    assert approvals.release(p.approval_id, None) == "noop"
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "rejected"


def test_the_api_login_cannot_write_an_action_or_a_check(engine, world):
    p = propose_follow_up(world)
    with pytest.raises(DBAPIError), db.transaction(world.user) as conn:
        conn.execute(
            text(
                "INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, guardian_version) "
                "VALUES (:a, :u, 'scope', true, 'x')"
            ),
            {"a": p.action_id, "u": world.user},
        )
    with pytest.raises(DBAPIError), db.transaction(world.user) as conn:
        conn.execute(
            text(
                "INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, status, summary) "
                "SELECT agent_run_id, user_id, 99, 'model_call', 'recorded', 'x' FROM agent_actions WHERE id = :a"
            ),
            {"a": p.action_id},
        )


# ---- at release: both keys run again ----


def after_proposal(engine, world, change):
    with engine.begin() as c:
        if change == "consent":
            trust.withdraw_consent(c, world.user, "ai_processing")
        elif change == "employer":
            exclude_company(c, world.user, "Lumen Demo")
        elif change == "constraint":
            c.execute(text("UPDATE jobs SET employment_type = 'full_time'"))
            event = c.execute(
                text(
                    "INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, "
                    "record_version_after) "
                    "VALUES (:u, 'applied', 'constraint', 'create', '{}'::jsonb, 'candidate_edit', 1) RETURNING id"
                ),
                {"u": world.user},
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO constraints (user_id, kind, statement, employment_types, source, profile_event_id) "
                    "VALUES (:u, 'employment_type', 'Contract only.', ARRAY['contract']::employment_type[], "
                    "'candidate_edit', :e)"
                ),
                {"u": world.user, "e": event},
            )
        elif change == "paused":
            c.execute(text("UPDATE campaigns SET paused_at = now()"))
        elif change == "never":
            c.execute(
                text(
                    "INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'send_follow_up', 'never')"
                ),
                {"u": world.user},
            )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("consent", "consent_missing"),
        ("employer", "employer_blocked"),
        ("constraint", "constraint_conflict"),
        ("paused", "campaign_paused"),
        ("never", "dial_never"),
    ],
)
def test_a_change_after_the_proposal_blocks_at_release_with_the_reason(engine, world, change, code):
    p = propose_follow_up(world)
    with db.transaction(world.user) as conn:
        guardian.decide(
            conn, world.user, p.approval_id, approve=True, payload_hash=guardian.payload_hash(follow_up_payload())
        )
    after_proposal(engine, world, change)
    assert approvals.release(p.approval_id, None) == "blocked"
    action = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(action) == ("blocked", code)
    assert scalar(engine, "SELECT count(*) FROM outbox") == 0
    assert (
        scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=p.action_id) == 12
    )  # both keys reran


# ---- at send: both keys run once more ----


@pytest.mark.parametrize("change", ["consent", "employer", "constraint", "paused", "never"])
def test_a_change_after_the_release_stops_the_message_at_the_relay(engine, world, smtp, change):
    p = propose_follow_up(world)
    approve_and_release(world, p)
    after_proposal(engine, world, change)
    make_due(engine, p.action_id, window_over=True)
    if change == "paused":  # a pause also holds the row; take the hold off to show the recheck is a key of its own
        with engine.begin() as c:
            c.execute(text("UPDATE outbox SET not_before = now() - interval '1 minute'"))
    assert relay_now() == 0 and smtp.messages == []
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "blocked"
    assert scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=p.action_id) == 18


# ---- every attempt is logged ----


@pytest.mark.parametrize(
    ("payload_to", "code"),
    [("someone.else@lumen.example", "restricted_data"), ("talent@lumen.example", None)],
)
def test_a_blocked_attempt_leaves_an_action_row_with_its_reason_and_a_failed_check(engine, world, payload_to, code):
    p = propose_follow_up(world, payload=follow_up_payload(to=payload_to))
    action = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE kind = 'outside_action'")
    assert len(action) == 1
    failed = rows(engine, "SELECT check_kind::text, reason_code FROM guardian_checks WHERE NOT passed")
    if code is None:
        assert (tuple(action[0]), failed) == (("awaiting_approval", None), [])
    else:
        assert tuple(action[0]) == ("blocked", code)
        assert [tuple(f) for f in failed] == [("scope", code)]
        assert p.status == "blocked"


def test_a_policy_denial_is_logged_with_its_reason_and_all_six_rows(engine, world):
    with engine.begin() as c:
        c.execute(text("UPDATE campaigns SET paused_at = now()"))
    p = propose_follow_up(world)
    action = rows(
        engine,
        "SELECT status::text, outcome_code, policy_decision::text FROM agent_actions WHERE id = :i",
        i=p.action_id,
    )[0]
    assert tuple(action) == ("blocked", "campaign_paused", "deny")
    assert scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=p.action_id) == 6
