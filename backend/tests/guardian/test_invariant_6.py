"""Invariant 6 (N4): nothing is sent twice. Each way a follow-up could go out twice produces at most one SMTP send
and one
outbox row for the action: the release job run twice, two relays draining at once, and a crash between the mail server
accepting the message and the row being marked."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.guardian import (
    approvals,
    jobs,  # noqa: F401
)
from app.platform import db, relay
from tests.guardian.support import (
    approve_and_release,
    entails,
    make_committed_world,
    make_due,
    propose_follow_up,
    relay_now,
    scalar,
)


@pytest.fixture
def due(engine, clean, smtp, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path, smtp=smtp)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    approve_and_release(world, p)
    make_due(engine, p.action_id)
    return world, p


def test_the_release_job_run_twice_leaves_one_outbox_row(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    approve_and_release(world, p)
    assert approvals.release(p.approval_id, None) == "noop"
    assert approvals.release(p.approval_id, None) == "noop"
    assert scalar(engine, "SELECT count(*) FROM outbox WHERE agent_action_id = :i", i=p.action_id) == 1


def test_two_releases_at_once_leave_one_outbox_row(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    from app.guardian import api as guardian
    from tests.guardian.support import follow_up_payload

    with db.transaction(world.user) as conn:
        guardian.decide(
            conn, world.user, p.approval_id, approve=True, payload_hash=guardian.payload_hash(follow_up_payload())
        )
    outcomes: list[str] = []
    threads = [
        threading.Thread(target=lambda: outcomes.append(approvals.release(p.approval_id, None))) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["noop", "scheduled"]
    assert scalar(engine, "SELECT count(*) FROM outbox") == 1


def test_a_second_outbox_row_for_the_action_is_refused_by_the_database(engine, due):
    world, p = due
    from sqlalchemy.exc import DBAPIError

    from app.platform import outbox

    with pytest.raises(DBAPIError), db.transaction(world.user, role="worker") as conn:
        outbox.add(
            conn,
            kind="follow_up_v1",
            dedupe_key="another-key",
            user_id=world.user,
            payload={"agent_action_id": p.action_id},
            not_before=None,
            agent_action_id=p.action_id,
        )
    assert scalar(engine, "SELECT count(*) FROM outbox") == 1


def test_two_relays_draining_at_once_send_it_once(engine, due, smtp):
    smtp.data_delay = 0.5  # the first relay is still talking to the server when the second looks
    counts: list[int] = []
    threads = [threading.Thread(target=lambda: counts.append(relay_now())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(counts) == [0, 1] and len(smtp.messages) == 1
    assert scalar(engine, "SELECT count(*) FROM outbox") == 1


def test_a_crash_after_the_server_accepted_the_message_never_sends_it_again(engine, due, smtp, monkeypatch):
    world, p = due
    real = relay._settle

    def die(*args, **kwargs):
        raise RuntimeError("the worker died right after the mail server said yes")

    monkeypatch.setattr(relay, "_settle", die)
    with pytest.raises(RuntimeError):
        relay_now()
    monkeypatch.setattr(relay, "_settle", real)
    assert len(smtp.messages) == 1
    assert scalar(engine, "SELECT status::text FROM outbox") == "sending"
    with engine.begin() as c:  # the lease runs out: the row becomes `unknown`, and nothing claims it again
        c.execute(text("UPDATE outbox SET locked_at = now() - :age"), {"age": relay.LEASE + timedelta(minutes=1)})
    assert relay_now() == 0 and relay_now() == 0
    assert scalar(engine, "SELECT status::text FROM outbox") == "unknown"
    assert len(smtp.messages) == 1
    assert scalar(engine, "SELECT count(*) FROM outbox WHERE agent_action_id = :i", i=p.action_id) == 1
    # the action stays scheduled until the operator settles the row
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"


def test_a_server_that_drops_the_connection_after_the_data_leaves_the_row_unknown(engine, due, smtp):
    smtp.drop_after_data = True
    assert relay_now() == 0
    assert len(smtp.messages) == 1
    with engine.begin() as c:
        c.execute(text("UPDATE outbox SET locked_at = now() - interval '10 minutes'"))
    relay_now()
    assert scalar(engine, "SELECT status::text FROM outbox") == "unknown"
    assert relay_now() == 0 and len(smtp.messages) == 1
