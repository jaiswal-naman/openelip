"""Plan 07-03, GRD-04: an approved follow-up waits in its undo window and can be undone until it ends; after sending
started nothing can be undone. The window is the person's own, from 1 to 60 minutes."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.platform import db
from app.platform.errors import ApiError
from tests.guardian.support import approve_and_release, entails, make_committed_world, propose_follow_up, rows, scalar


@pytest.fixture
def scheduled(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    assert approve_and_release(world, p) == "scheduled"
    return world, p


def undo(world, action_id):
    with db.transaction(world.user) as conn:
        return guardian.undo(conn, world.user, action_id)


def test_undo_inside_the_window_cancels_the_action_and_its_message(engine, scheduled):
    world, p = scheduled
    result = undo(world, p.action_id)
    assert (result.status, result.outbox_cancelled) == ("cancelled", 1)
    action = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(action) == ("cancelled", "undone")
    message = rows(
        engine, "SELECT status::text, last_error_code FROM outbox WHERE agent_action_id = :i", i=p.action_id
    )[0]
    assert tuple(message) == ("cancelled", "undone")


@pytest.mark.parametrize("minutes", [1, 60])
def test_the_window_is_the_persons_own_one_to_sixty_minutes(engine, clean, monkeypatch, tmp_path, minutes):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET undo_window_minutes = :m WHERE id = :u"), {"m": minutes, "u": world.user})
    p = propose_follow_up(world)
    approve_and_release(world, p)
    left = scalar(
        engine,
        "SELECT extract(epoch FROM undo_until - clock_timestamp()) / 60 FROM agent_actions WHERE id = :i",
        i=p.action_id,
    )
    assert minutes - 0.5 < float(left) <= minutes
    assert undo(world, p.action_id).status == "cancelled"


def test_once_the_message_is_due_undo_is_a_conflict(engine, scheduled):
    world, p = scheduled
    with engine.begin() as c:  # the window is over: the message is due
        c.execute(
            text("UPDATE outbox SET not_before = now() - interval '1 second' WHERE agent_action_id = :i"),
            {"i": p.action_id},
        )
    with pytest.raises(ApiError) as caught:
        undo(world, p.action_id)
    assert (caught.value.status, caught.value.code, caught.value.details) == (409, "conflict", {"state": "sending"})
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "scheduled"


@pytest.mark.parametrize(("message", "state"), [("sending", "sending"), ("sent", "sent"), ("unknown", "sending")])
def test_after_sending_started_undo_reports_the_state(engine, scheduled, message, state):
    world, p = scheduled
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE outbox SET status = CAST(:s AS outbox_status), not_before = now() - interval '1 minute', "
                "sent_at = CASE WHEN :s = 'sent' THEN now() END WHERE agent_action_id = :i"
            ),
            {"s": message, "i": p.action_id},
        )
    with pytest.raises(ApiError) as caught:
        undo(world, p.action_id)
    assert caught.value.details == {"state": state}


def test_an_executed_action_cannot_be_undone(engine, scheduled):
    world, p = scheduled
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(
            text("UPDATE agent_actions SET status = 'executed', executed_at = now() WHERE id = :i"), {"i": p.action_id}
        )
    with pytest.raises(ApiError) as caught:
        undo(world, p.action_id)
    assert caught.value.details == {"state": "executed"}


def test_another_persons_action_is_not_found(engine, scheduled):
    world, p = scheduled
    other = make_committed_world(engine, consent=False)
    with pytest.raises(ApiError) as caught:
        undo(other, p.action_id)
    assert caught.value.status == 404


def test_a_held_follow_up_can_still_be_undone(engine, scheduled):
    world, p = scheduled
    with db.transaction(world.user) as conn:
        assert guardian.hold(conn, world.campaign) == 1
    assert undo(world, p.action_id).status == "cancelled"
