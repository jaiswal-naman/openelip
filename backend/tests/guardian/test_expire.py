"""Plan 07-03, CMP-07: unanswered approvals expire, and a cancelled campaign's open work is cancelled."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import approvals
from app.platform import db
from tests.guardian.support import approve_and_release, entails, make_committed_world, propose_follow_up, rows, scalar


@pytest.fixture
def waiting(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    return world, propose_follow_up(world)


def test_the_sweep_expires_approvals_past_their_time_and_their_actions(engine, waiting):
    world, p = waiting
    with db.transaction(role="worker") as conn:
        assert approvals.expire_due(conn) == 0  # not yet due
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("UPDATE approvals SET expires_at = now() - interval '1 minute'"))
    with db.transaction(role="worker") as conn:
        assert approvals.expire_due(conn) == 1
        assert approvals.expire_due(conn) == 0
    assert scalar(engine, "SELECT status::text FROM approvals WHERE id = :a", a=p.approval_id) == "expired"
    action = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(action) == ("expired", "expired")


def test_cancel_open_expires_pending_approvals_and_cancels_scheduled_work(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    first = propose_follow_up(world, step="follow_up:1")
    approve_and_release(world, first)
    second = propose_follow_up(world, step="follow_up:2")
    with db.transaction(world.user) as conn:
        counts = guardian.cancel_open(conn, world.campaign, "campaign cancelled")
    assert counts == {"approvals_expired": 1, "actions_cancelled": 2, "messages_cancelled": 1}
    states = rows(
        engine, "SELECT id, status::text, outcome_code FROM agent_actions WHERE kind = 'outside_action' ORDER BY id"
    )
    assert [tuple(s[1:]) for s in states] == [("cancelled", "cancelled"), ("cancelled", "cancelled")]
    approval = rows(engine, "SELECT status::text, note FROM approvals WHERE id = :a", a=second.approval_id)[0]
    assert tuple(approval) == ("expired", "campaign cancelled")
    assert scalar(engine, "SELECT status::text FROM outbox") == "cancelled"


def test_cancel_open_leaves_a_message_that_already_started_sending(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    approve_and_release(world, p)
    with engine.begin() as c:
        c.execute(text("UPDATE outbox SET status = 'sending', not_before = now() - interval '1 minute'"))
    with db.transaction(world.user) as conn:
        counts = guardian.cancel_open(conn, world.campaign, "campaign cancelled")
    assert counts["actions_cancelled"] == 0
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"
