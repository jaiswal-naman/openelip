"""Plan 07-01: an outbox message that is an agent's outside action can be held, released and cancelled by action, and
the relay does not claim a held one."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.platform import outbox, relay
from tests.campaigns.factories import make_action, make_approval, make_check, make_run, make_world


def scheduled_follow_up(conn, world, minutes: int = 10) -> int:
    run = make_run(conn, world.user, world.campaign, "chaser")
    action = make_action(conn, run, kind="outside_action", action_type="send_follow_up", status="proposed")
    make_check(conn, action, "scope", True)
    make_approval(conn, action, status="approved")
    conn.execute(
        text(
            """UPDATE agent_actions SET status = 'scheduled', undo_until = now() + make_interval(mins => :m)
               WHERE id = :a"""
        ),
        {"a": action, "m": minutes},
    )
    return action


def queue_message(conn, world, action: int, *, offset_minutes: int = 10):
    undo = conn.execute(text("SELECT undo_until FROM agent_actions WHERE id = :a"), {"a": action}).scalar_one()
    return outbox.add(
        conn,
        kind="follow_up_v1",
        dedupe_key=f"action:{action}",
        user_id=world.user,
        payload={"agent_action_id": action},
        not_before=undo + timedelta(minutes=offset_minutes - 10),
        agent_action_id=action,
    )


def row(conn, message):
    return conn.execute(
        text(
            """SELECT status::text AS status, not_before::text AS not_before, agent_action_id, last_error_code
               FROM outbox WHERE id = :i"""
        ),
        {"i": message},
    ).one()


def test_add_stores_the_action_id_and_leaves_other_messages_without_one(conn):
    world = make_world(conn)
    action = scheduled_follow_up(conn, world)
    message = queue_message(conn, world, action)
    assert row(conn, message).agent_action_id == action
    brief = outbox.add(conn, kind="brief_v1", dedupe_key="b1", user_id=world.user, payload={})
    assert row(conn, brief).agent_action_id is None


def test_add_for_an_action_that_is_not_scheduled_is_refused_by_the_database(conn):
    from sqlalchemy.exc import DBAPIError

    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "chaser")
    action = make_action(conn, run, kind="outside_action", action_type="send_follow_up", status="proposed")
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError, match="two_key"):
        outbox.add(conn, kind="follow_up_v1", dedupe_key="x", user_id=world.user, payload={}, agent_action_id=action)
    savepoint.rollback()


def test_action_states_returns_the_status_of_each_actions_message_and_leaves_out_the_rest(conn):
    world = make_world(conn)
    sent, waiting = scheduled_follow_up(conn, world), scheduled_follow_up(conn, world)
    plain = scheduled_follow_up(conn, world)
    one, two = queue_message(conn, world, sent), queue_message(conn, world, waiting)
    conn.execute(text("UPDATE outbox SET status = 'sent', sent_at = now() WHERE id = :i"), {"i": one})
    assert outbox.action_states(conn, [sent, waiting, plain]) == {sent: "sent", waiting: "pending"}
    assert outbox.action_states(conn, []) == {}
    assert two


def test_hold_makes_pending_rows_wait_forever_and_the_relay_does_not_claim_them(conn):
    world = make_world(conn)
    action = scheduled_follow_up(conn, world)
    message = queue_message(conn, world, action)
    conn.execute(text("UPDATE outbox SET not_before = now() - interval '1 second' WHERE id = :i"), {"i": message})
    assert outbox.hold(conn, [action]) == 1
    assert row(conn, message).not_before == "infinity"  # read as text: psycopg cannot load infinity as a datetime
    assert outbox.hold(conn, []) == 0
    conn.execute(text("UPDATE outbox SET status = 'sent', sent_at = now() WHERE id = :i"), {"i": message})
    assert outbox.hold(conn, [action]) == 0  # a message that was sent is not held


def test_a_held_message_is_not_claimed_by_the_relay_and_a_released_one_is(conn):
    world = make_world(conn)
    action = scheduled_follow_up(conn, world)
    message = queue_message(conn, world, action)
    conn.execute(text("UPDATE outbox SET not_before = now() - interval '1 second' WHERE id = :i"), {"i": message})
    outbox.hold(conn, [action])
    assert relay._claim(conn) is None  # the relay's own claim query: nothing is due
    outbox.release(conn, [action], datetime.now(timezone.utc) - timedelta(seconds=1))
    claimed = relay._claim(conn)
    assert claimed is not None and claimed.id == message


def test_release_sets_a_finite_time_and_only_for_pending_rows(conn):
    world = make_world(conn)
    action = scheduled_follow_up(conn, world)
    message = queue_message(conn, world, action)
    outbox.hold(conn, [action])
    when = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert outbox.release(conn, [action], when) == 1
    assert conn.execute(
        text("SELECT not_before = :w FROM outbox WHERE id = :i"), {"w": when, "i": message}
    ).scalar_one()
    conn.execute(text("UPDATE outbox SET status = 'sending' WHERE id = :i"), {"i": message})
    assert outbox.release(conn, [action], when + timedelta(hours=1)) == 0
    assert outbox.release(conn, [], when) == 0


def test_cancel_for_actions_cancels_pending_rows_only_and_returns_their_ids(conn):
    world = make_world(conn)
    mine, started, plain = (
        scheduled_follow_up(conn, world),
        scheduled_follow_up(conn, world),
        scheduled_follow_up(conn, world),
    )
    pending_message = queue_message(conn, world, mine)
    sending_message = queue_message(conn, world, started)
    conn.execute(text("UPDATE outbox SET status = 'sending' WHERE id = :i"), {"i": sending_message})
    assert outbox.cancel_for_actions(conn, [mine, started, plain]) == [pending_message]
    assert (row(conn, pending_message).status, row(conn, pending_message).last_error_code) == ("cancelled", "undone")
    assert row(conn, sending_message).status == "sending"  # once sending started it is not undone
    assert outbox.cancel_for_actions(conn, [mine]) == []  # nothing pending is left
    assert outbox.cancel_for_actions(conn, []) == []


def test_cancel_for_actions_takes_the_given_code(conn):
    world = make_world(conn)
    action = scheduled_follow_up(conn, world)
    message = queue_message(conn, world, action)
    assert outbox.cancel_for_actions(conn, [action], "scope_changed") == [message]
    assert row(conn, message).last_error_code == "scope_changed"
