"""Plan 07-03: the settle sweep moves a follow-up to the state its message reached, and the schedules and queue handlers
exist."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import approvals, jobs
from app.platform import db, events, queue, scheduler
from tests.guardian.support import (
    approve_and_release,
    entails,
    make_committed_world,
    make_due,
    propose_follow_up,
    relay_now,
    rows,
    scalar,
)


@pytest.fixture
def sent_once(engine, clean, smtp, monkeypatch, tmp_path):
    monkeypatch.setattr(events, "_subscribers", {"guardian.action_changed": [("campaign.resume", "default")]})
    entails(monkeypatch, tmp_path, smtp=smtp)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    approve_and_release(world, p)
    make_due(engine, p.action_id)
    return world, p


def settle() -> int:
    with db.transaction(role="worker") as conn:
        return approvals.settle(conn)


def test_a_sent_message_makes_its_action_executed_and_publishes_the_change(engine, sent_once, smtp):
    world, p = sent_once
    assert relay_now() == 1
    assert settle() == 1 and settle() == 0
    row = rows(engine, "SELECT status::text, executed_at IS NOT NULL FROM agent_actions WHERE id = :i", i=p.action_id)[
        0
    ]
    assert tuple(row) == ("executed", True)
    keys = {r[0] for r in rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume'")}
    assert f"campaign.resume:{world.campaign}:a{p.action_id}.executed" in keys


def test_a_failed_message_makes_its_action_failed_with_the_outbox_code(engine, sent_once, smtp):
    world, p = sent_once
    smtp.rcpt_reply = (550, "no such mailbox")
    assert relay_now() == 0
    assert settle() == 1
    row = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(row) == ("failed", "smtp_permanent")


@pytest.mark.parametrize("state", ["pending", "sending", "unknown"])
def test_a_message_still_pending_or_of_unknown_outcome_leaves_the_action_scheduled(engine, sent_once, state):
    world, p = sent_once
    with engine.begin() as c:
        c.execute(text("UPDATE outbox SET status = CAST(:s AS outbox_status)"), {"s": state})
    assert settle() == 0
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"


def test_a_message_the_platform_cancelled_makes_its_action_cancelled(engine, sent_once):
    world, p = sent_once
    with engine.begin() as c:
        c.execute(text("UPDATE outbox SET status = 'cancelled', last_error_code = 'expired'"))
    assert settle() == 1
    row = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(row) == ("cancelled", "expired")


def test_a_dial_set_to_never_after_sending_does_not_stop_the_sweep(engine, sent_once, smtp):
    world, p = sent_once
    assert relay_now() == 1
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'send_follow_up', 'never')"),
            {"u": world.user},
        )
    assert settle() == 0  # the trigger refuses `executed`; the sweep logs it and carries on
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"


def test_the_handlers_and_the_two_sweeps_are_registered():
    assert {"guardian.release", "guardian.expire", "guardian.settle"} <= set(queue._handlers)
    assert scheduler._every["guardian.expire"] == 60 and scheduler._every["guardian.settle"] == 60
    assert jobs.release is queue._handlers["guardian.release"]
    assert guardian.release is approvals.release


def test_the_sweep_handlers_run_on_a_worker_connection(engine, sent_once, smtp):
    world, p = sent_once
    relay_now()
    with db.transaction(role="worker") as conn:
        jobs.settle(conn, {})
        jobs.expire(conn, {})
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "executed"
