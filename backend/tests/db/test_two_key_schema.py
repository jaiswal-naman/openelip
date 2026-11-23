"""Invariants 5 and 6 at the database (plan 07-01; DATA_MODEL 4.6 and 12.5 T5): no outside action is scheduled or
executed without a policy that allows it, Guardian checks that all passed, a dial other than never and an approval the
candidate gave, an outbox row exists only for a scheduled action and not before its undo window ends, and there is never
a second outbox row for one action. One test per forbidden move, and the permitted move beside it.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Rows are built with
tests/campaigns/factories.py; the moves under test are plain UPDATE statements with no app.maintenance flag, so the
trigger decides.
"""

import json
from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import World, make_action, make_approval, make_check, make_run, make_world


@contextmanager
def refused(conn: Connection):
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def status_of(conn: Connection, action: int) -> str:
    return one(conn, "SELECT status::text FROM agent_actions WHERE id = :a", a=action)


def schedule(conn: Connection, action: int, undo_minutes: int | None = None) -> None:
    """The move to scheduled, as the worker makes it; a follow-up needs its undo window set on the same statement."""
    conn.execute(
        text(
            """UPDATE agent_actions SET status = 'scheduled',
                      undo_until = CASE WHEN CAST(:m AS int) IS NULL THEN NULL
                                        ELSE now() + make_interval(mins => CAST(:m AS int)) END
               WHERE id = :a"""
        ),
        {"a": action, "m": undo_minutes},
    )


def proposed(conn: Connection, world: World, action_type: str = "submit_application", **kwargs) -> int:
    run = make_run(conn, world.user, world.campaign, "operator")
    return make_action(conn, run, kind="outside_action", action_type=action_type, status="proposed", **kwargs)


def cleared(conn: Connection, world: World, action_type: str = "submit_application") -> int:
    """A proposed action with every key turned: passing checks and an approved approval."""
    action = proposed(conn, world, action_type)
    for kind in ("citations", "consent", "scope", "budget", "injection", "rate_limit"):
        make_check(conn, action, kind, True)
    make_approval(conn, action, status="approved")
    return action


def maintenance(conn: Connection, sql: str, **params) -> None:
    """A statement under app.maintenance, which only erase_user and purge_expired set in the running system: used here
    to put a row in a state the guard would not let a test reach, never to test the guard."""
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text(sql), params)
    conn.execute(text("SELECT set_config('app.maintenance', '', true)"))


# ---- invariant 5: the two keys --------------------------------------------------------------------------------------


def test_an_action_cannot_be_inserted_already_scheduled_or_executed(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "operator")
    for status in ("scheduled", "executed", "awaiting_approval", "blocked"):
        with refused(conn) as caught:
            conn.execute(
                text(
                    """INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, action_type, status, summary, payload,
                                                  policy_decision, policy_version, outcome_code, executed_at, undo_until)
                       VALUES (:r, :u, 1, 'outside_action', 'submit_application', CAST(:s AS action_status), 's', '{}',
                               'ask', 'v', 'x', CASE WHEN :s = 'executed' THEN now() END, NULL)"""
                ),
                {"r": run, "u": world.user, "s": status},
            )
        assert "recorded first" in str(caught.value), status


def test_proposed_to_scheduled_with_no_checks_is_refused(conn):
    action = proposed(conn, make_world(conn))
    make_approval(conn, action, status="approved")
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the Guardian has not passed" in str(caught.value)
    assert status_of(conn, action) == "proposed"


def test_a_failed_check_among_passing_ones_is_refused(conn):
    action = proposed(conn, make_world(conn))
    make_check(conn, action, "scope", True)
    make_check(conn, action, "budget", False, "budget_exhausted")
    make_approval(conn, action, status="approved")
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the Guardian has not passed" in str(caught.value)


def test_passing_checks_but_no_approval_is_refused(conn):
    action = proposed(conn, make_world(conn))
    make_check(conn, action, "scope", True)
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the candidate has not approved" in str(caught.value)


@pytest.mark.parametrize("status", ["pending", "rejected", "expired"])
def test_an_approval_that_is_not_approved_is_refused(conn, status):
    action = proposed(conn, make_world(conn))
    make_check(conn, action, "scope", True)
    make_approval(conn, action, status=status)
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the candidate has not approved" in str(caught.value)


def test_a_dial_of_never_refuses_even_with_both_keys(conn):
    world = make_world(conn)
    action = cleared(conn, world)
    conn.execute(
        text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'submit_application', 'never')"),
        {"u": world.user},
    )
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the candidate set submit_application to never" in str(caught.value)


def test_a_policy_decision_of_deny_refuses_even_with_both_keys(conn):
    world = make_world(conn)
    action = proposed(conn, world, policy_decision="deny")
    make_check(conn, action, "scope", True)
    make_approval(conn, action, status="approved")
    with refused(conn) as caught:
        schedule(conn, action)
    assert "two_key: the policy engine denied" in str(caught.value)


def test_a_denied_action_can_be_blocked_but_never_cleared(conn):
    action = proposed(conn, make_world(conn), policy_decision="deny")
    conn.execute(text("UPDATE agent_actions SET status = 'blocked', outcome_code = 'policy_deny' WHERE id = :a"), {"a": action})
    assert status_of(conn, action) == "blocked"


def test_with_a_policy_that_allows_passing_checks_and_an_approval_the_move_succeeds(conn):
    world = make_world(conn)
    action = cleared(conn, world)
    schedule(conn, action)
    assert status_of(conn, action) == "scheduled"
    conn.execute(text("UPDATE agent_actions SET status = 'executed', executed_at = now() WHERE id = :a"), {"a": action})
    assert status_of(conn, action) == "executed"


def twin_action(conn: Connection, world: World, policy_decision: str) -> int:
    run = make_run(conn, world.user, world.campaign, "twin")
    action = make_action(
        conn,
        run,
        kind="outside_action",
        action_type="twin_answer",
        status="proposed",
        payload={"answer": "invented"},
        policy_decision=policy_decision,
    )
    make_check(conn, action, "scope", True)
    return action


def set_dial(conn: Connection, world: World, level: str) -> None:
    conn.execute(
        text(
            "INSERT INTO autonomy_settings (user_id, action_type, level) "
            "VALUES (:u, 'twin_answer', CAST(:l AS autonomy_level))"
        ),
        {"u": world.user, "l": level},
    )


def test_a_twin_answer_needs_the_approval_unless_the_policy_allows_it_and_the_dial_is_tell_me_after(conn):
    world = make_world(conn)
    allowed = twin_action(conn, world, "allow")
    with refused(conn) as caught:  # no dial row means ask_first
        schedule(conn, allowed)
    assert "has not approved" in str(caught.value)
    asked = twin_action(conn, world, "ask")
    set_dial(conn, world, "tell_me_after")
    with refused(conn) as caught:  # the policy engine said ask, whatever the dial says
        schedule(conn, asked)
    assert "has not approved" in str(caught.value)
    schedule(conn, allowed)  # policy allow and a tell_me_after dial: the one path with no approval
    assert status_of(conn, allowed) == "scheduled"


# ---- the moves and the immutability ---------------------------------------------------------------------------------


def scheduled(conn: Connection, world: World) -> int:
    action = cleared(conn, world)
    schedule(conn, action)
    return action


@pytest.mark.parametrize("end", ["proposed", "awaiting_approval", "rejected", "expired"])
def test_a_scheduled_action_never_moves_backwards(conn, end):
    action = scheduled(conn, make_world(conn))
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE agent_actions SET status = CAST(:e AS action_status), outcome_code = 'x' WHERE id = :a"),
            {"a": action, "e": end},
        )
    assert "cannot move from" in str(caught.value)


@pytest.mark.parametrize("end_state", ["executed", "cancelled", "blocked", "failed"])
def test_an_end_state_is_final(conn, end_state):
    action = scheduled(conn, make_world(conn))
    conn.execute(
        text(
            """UPDATE agent_actions SET status = CAST(:s AS action_status), outcome_code = 'x',
                      executed_at = CASE WHEN :s = 'executed' THEN now() END WHERE id = :a"""
        ),
        {"a": action, "s": end_state},
    )
    for back in (state for state in ("scheduled", "proposed", "executed", "cancelled") if state != end_state):
        with refused(conn) as caught:
            conn.execute(
                text(
                    """UPDATE agent_actions SET status = CAST(:b AS action_status),
                              executed_at = CASE WHEN :b = 'executed' THEN now() ELSE executed_at END WHERE id = :a"""
                ),
                {"a": action, "b": back},
            )
        assert "cannot move from" in str(caught.value), (end_state, back)


@pytest.mark.parametrize("end", ["rejected", "expired", "cancelled", "blocked"])
def test_an_awaiting_action_can_end_four_ways_and_never_be_scheduled_afterwards(conn, end):
    world = make_world(conn)
    action = cleared(conn, world)
    conn.execute(text("UPDATE agent_actions SET status = 'awaiting_approval' WHERE id = :a"), {"a": action})
    conn.execute(
        text("UPDATE agent_actions SET status = CAST(:e AS action_status), outcome_code = 'x' WHERE id = :a"),
        {"a": action, "e": end},
    )
    with refused(conn) as caught:
        schedule(conn, action)
    assert "cannot move from" in str(caught.value)


@pytest.mark.parametrize(
    "column, value",
    [
        ("summary", "'rewritten'"),
        ("payload", "'{\"to\": \"someone@else.example\"}'::jsonb"),
        ("inputs", "'{\"x\": 1}'::jsonb"),
        ("evidence", "'[1]'::jsonb"),
        ("policy_decision", "'allow'"),
        ("policy_version", "'other'"),
        ("action_type", "'send_follow_up'"),
        ("seq", "99"),
                ("created_at", "now() + interval '1 day'"),
    ],
)
def test_the_content_of_an_action_never_changes(conn, column, value):
    action = proposed(conn, make_world(conn))
    with refused(conn) as caught:
        conn.execute(text(f"UPDATE agent_actions SET {column} = {value} WHERE id = :a"), {"a": action})
    assert "immutable" in str(caught.value), column


def test_content_stays_fixed_after_scheduling_too(conn):
    action = scheduled(conn, make_world(conn))
    for sql in (
        "UPDATE agent_actions SET summary = 'rewritten' WHERE id = :a",
        "UPDATE agent_actions SET payload = '{}' WHERE id = :a",
    ):
        with refused(conn) as caught:
            conn.execute(text(sql), {"a": action})
        assert "immutable" in str(caught.value)


def test_an_action_is_never_deleted_and_a_recorded_row_does_not_move(conn):
    world = make_world(conn)
    action = proposed(conn, world)
    with refused(conn) as caught:
        conn.execute(text("DELETE FROM agent_actions WHERE id = :a"), {"a": action})
    assert "never deleted" in str(caught.value)
    run = make_run(conn, world.user, world.campaign, "researcher")
    step = make_action(conn, run, kind="tool_call", status="recorded")
    with refused(conn):
        conn.execute(text("UPDATE agent_actions SET status = 'scheduled' WHERE id = :a"), {"a": step})


@pytest.mark.parametrize("end", ["awaiting_approval", "blocked"])
def test_a_proposed_action_may_wait_or_be_blocked(conn, end):
    action = proposed(conn, make_world(conn))
    conn.execute(
        text("UPDATE agent_actions SET status = CAST(:e AS action_status), outcome_code = 'x' WHERE id = :a"),
        {"a": action, "e": end},
    )
    assert status_of(conn, action) == end


@pytest.mark.parametrize("end", ["rejected", "expired", "cancelled", "failed"])
def test_a_proposed_action_cannot_skip_to_an_end_state(conn, end):
    action = proposed(conn, make_world(conn))
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE agent_actions SET status = CAST(:e AS action_status), outcome_code = 'x' WHERE id = :a"),
            {"a": action, "e": end},
        )
    assert "cannot move from" in str(caught.value)


# ---- the undo window (GRD-04, decision 3) ----------------------------------------------------------------------------


def test_a_follow_up_may_be_proposed_with_no_undo_time_but_not_scheduled_without_one(conn):
    action = cleared(conn, make_world(conn), "send_follow_up")
    assert one(conn, "SELECT undo_until FROM agent_actions WHERE id = :a", a=action) is None
    with refused(conn) as caught:
        schedule(conn, action)  # undo_until still NULL
    assert "agent_actions_check" in str(caught.value) or "check constraint" in str(caught.value)
    assert status_of(conn, action) == "proposed"
    schedule(conn, action, undo_minutes=10)
    assert status_of(conn, action) == "scheduled"
    assert one(conn, "SELECT undo_until > now() + interval '9 minutes' FROM agent_actions WHERE id = :a", a=action)


@pytest.mark.parametrize("seconds", [10, 59, 0, -3600])
def test_the_undo_time_must_be_at_least_a_minute_ahead(conn, seconds):
    action = cleared(conn, make_world(conn), "send_follow_up")
    with refused(conn) as caught:
        conn.execute(
            text(
                "UPDATE agent_actions SET status = 'scheduled', undo_until = now() + make_interval(secs => :s) "
                "WHERE id = :a"
            ),
            {"a": action, "s": seconds},
        )
    assert "one minute ahead" in str(caught.value)


@pytest.mark.parametrize("minutes", [1, 10, 60])
def test_a_one_minute_ten_minute_and_sixty_minute_window_are_all_accepted(conn, minutes):
    action = cleared(conn, make_world(conn), "send_follow_up")
    schedule(conn, action, undo_minutes=minutes)
    assert status_of(conn, action) == "scheduled"


def test_the_undo_time_is_set_once_and_never_changes(conn):
    action = cleared(conn, make_world(conn), "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    for sql in (
        "UPDATE agent_actions SET undo_until = now() + interval '30 minutes' WHERE id = :a",
        "UPDATE agent_actions SET undo_until = NULL WHERE id = :a",
        "UPDATE agent_actions SET undo_until = undo_until - interval '9 minutes' WHERE id = :a",
    ):
        with refused(conn) as caught:
            conn.execute(text(sql), {"a": action})
        assert "set once" in str(caught.value)


def test_the_undo_time_cannot_be_set_on_a_row_that_is_not_moving_to_scheduled(conn):
    world = make_world(conn)
    action = proposed(conn, world, "send_follow_up")
    with refused(conn) as caught:  # still proposed
        conn.execute(
            text("UPDATE agent_actions SET undo_until = now() + interval '10 minutes' WHERE id = :a"), {"a": action}
        )
    assert "only when the action is scheduled" in str(caught.value)
    blocked = proposed(conn, world, "send_follow_up")
    with refused(conn) as caught:  # moving to blocked
        conn.execute(
            text(
                "UPDATE agent_actions SET status = 'blocked', outcome_code = 'x', "
                "undo_until = now() + interval '10 minutes' WHERE id = :a"
            ),
            {"a": blocked},
        )
    assert "only when the action is scheduled" in str(caught.value)


def test_executing_before_the_undo_time_is_refused_and_after_it_is_allowed(conn):
    action = cleared(conn, make_world(conn), "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE agent_actions SET status = 'executed', executed_at = now() WHERE id = :a"), {"a": action}
        )
    assert "undo window is still open" in str(caught.value)
    maintenance(conn, "UPDATE agent_actions SET undo_until = now() - interval '1 second' WHERE id = :a", a=action)
    conn.execute(text("UPDATE agent_actions SET status = 'executed', executed_at = now() WHERE id = :a"), {"a": action})
    assert status_of(conn, action) == "executed"


def test_a_submit_application_has_no_undo_window(conn):
    action = scheduled(conn, make_world(conn))
    assert one(conn, "SELECT undo_until FROM agent_actions WHERE id = :a", a=action) is None
    conn.execute(
        text("UPDATE agent_actions SET status = 'executed', executed_at = now() WHERE id = :a"), {"a": action}
    )
    assert status_of(conn, action) == "executed"


def test_a_scheduled_follow_up_with_no_undo_time_cannot_exist_even_through_maintenance(conn):
    action = cleared(conn, make_world(conn), "send_follow_up")
    with refused(conn):
        maintenance(conn, "UPDATE agent_actions SET status = 'scheduled' WHERE id = :a", a=action)


# ---- the outbox guard ------------------------------------------------------------------------------------------------


def add_outbox(conn: Connection, user: UUID, action: int, dedupe: str, not_before: str = "now()") -> None:
    conn.execute(
        text(
            f"""INSERT INTO outbox (dedupe_key, user_id, template, payload, agent_action_id, not_before)
                VALUES (:d, :u, 'follow_up_v1', CAST(:p AS jsonb), :a, {not_before})"""
        ),
        {"d": dedupe, "u": user, "p": json.dumps({"agent_action_id": action}), "a": action},
    )


def test_no_outbox_row_for_an_action_that_is_not_scheduled(conn):
    world = make_world(conn)
    action = proposed(conn, world, "send_follow_up")
    with refused(conn) as caught:
        add_outbox(conn, world.user, action, f"action:{action}", "now() + interval '1 hour'")
    assert "two_key: outbox rows" in str(caught.value)
    waiting = cleared(conn, world, "send_follow_up")
    conn.execute(text("UPDATE agent_actions SET status = 'awaiting_approval' WHERE id = :a"), {"a": waiting})
    with refused(conn) as caught:
        add_outbox(conn, world.user, waiting, f"action:{waiting}", "now() + interval '1 hour'")
    assert "two_key: outbox rows" in str(caught.value)


def test_no_outbox_row_before_the_undo_window_ends(conn):
    world = make_world(conn)
    action = cleared(conn, world, "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    for not_before in ("now()", "now() + interval '5 minutes'"):
        with refused(conn) as caught:
            add_outbox(conn, world.user, action, f"action:{action}", not_before)
        assert "two_key: outbox rows" in str(caught.value)


def test_an_outbox_row_at_the_undo_time_is_accepted(conn):
    world = make_world(conn)
    action = cleared(conn, world, "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    undo = one(conn, "SELECT undo_until FROM agent_actions WHERE id = :a", a=action)
    conn.execute(
        text(
            """INSERT INTO outbox (dedupe_key, user_id, template, payload, agent_action_id, not_before)
               VALUES (:d, :u, 'follow_up_v1', '{}', :a, :n)"""
        ),
        {"d": f"action:{action}", "u": world.user, "a": action, "n": undo},
    )
    assert one(conn, "SELECT count(*) FROM outbox WHERE agent_action_id = :a", a=action) == 1


# ---- invariant 6: one send per action --------------------------------------------------------------------------------


def test_a_second_outbox_row_for_one_action_is_refused_even_under_another_key(conn):
    world = make_world(conn)
    action = cleared(conn, world, "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    later = "now() + interval '11 minutes'"
    add_outbox(conn, world.user, action, f"action:{action}", later)
    with refused(conn) as caught:
        add_outbox(conn, world.user, action, f"action:{action}:again", later)
    assert "outbox_one_per_action" in str(caught.value)
    with refused(conn) as caught:  # the same key too
        add_outbox(conn, world.user, action, f"action:{action}", later)
    assert "outbox_dedupe" in str(caught.value) or "outbox_one_per_action" in str(caught.value)


def test_an_outbox_row_for_another_persons_action_is_refused(conn):
    world, other = make_world(conn), make_world(conn)
    action = cleared(conn, world, "send_follow_up")
    schedule(conn, action, undo_minutes=10)
    with refused(conn) as caught:
        add_outbox(conn, other.user, action, f"action:{action}", "now() + interval '11 minutes'")
    assert "two_key: outbox rows" in str(caught.value)


def test_an_email_that_is_not_an_agent_action_is_not_held_to_these_rules(conn):
    world = make_world(conn)
    conn.execute(
        text("INSERT INTO outbox (dedupe_key, user_id, template, payload) VALUES (:d, :u, 'brief_v1', '{}')"),
        {"d": "brief:test", "u": world.user},
    )
    assert one(conn, "SELECT count(*) FROM outbox WHERE dedupe_key = 'brief:test'") == 1
