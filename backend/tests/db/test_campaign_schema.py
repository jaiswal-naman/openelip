"""Revision p7_0701_campaigns_guardian, the tables (plan 07-01): one open campaign per job, one step per key, the
dossier citation CHECK, the autonomy locks, approvals, the append-only checks, record_state_change, and who may read or
write what.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Rows are invented
(tests/campaigns/factories.py) and written with plain SQL as the bootstrap login, which bypasses row-level security; a
role is taken on with SET LOCAL ROLE when the test is about what that role may do.
"""

from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import (
    World,
    make_action,
    make_approval,
    make_campaign,
    make_check,
    make_dossier,
    make_run,
    make_step,
    make_world,
)
from tests.match.factories import make_job, make_user

CAMPAIGN_TABLES = (
    "campaigns",
    "campaign_steps",
    "company_dossiers",
    "agent_runs",
    "agent_actions",
    "approvals",
    "autonomy_settings",
    "guardian_checks",
)


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def count(conn: Connection, source: str, **params) -> int:
    return one(conn, f"SELECT count(*) FROM {source}", **params)


def act_as(conn: Connection, role: str, user_id: UUID | None = None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


def back_to_bootstrap(conn: Connection) -> None:
    conn.execute(text("RESET ROLE"))


# ---- campaigns -------------------------------------------------------------------------------------------------------


def test_a_second_open_campaign_for_one_job_is_refused_and_a_closed_one_makes_room(conn):
    world = make_world(conn)
    with refused(conn) as caught:
        make_campaign(conn, world.user, world.job.id)
    assert "campaigns_one_open_per_job" in str(caught.value)
    conn.execute(
        text("UPDATE campaigns SET stage = 'closed', closed_reason = 'cancelled', closed_at = now() WHERE id = :c"),
        {"c": world.campaign},
    )
    assert make_campaign(conn, world.user, world.job.id)  # accepted now
    other = make_user(conn)
    assert make_campaign(conn, other, world.job.id)  # another person may have an open campaign for the same job


def test_closing_needs_both_a_time_and_a_reason_and_nothing_else_may_have_them(conn):
    world = make_world(conn)
    for sql in (
        "UPDATE campaigns SET stage = 'closed' WHERE id = :c",
        "UPDATE campaigns SET stage = 'closed', closed_at = now() WHERE id = :c",
        "UPDATE campaigns SET closed_reason = 'cancelled', closed_at = now() WHERE id = :c",
    ):
        with refused(conn) as caught:
            conn.execute(text(sql), {"c": world.campaign})
        assert "check" in str(caught.value).lower()


def test_a_closed_campaign_cannot_be_paused(conn):
    world = make_world(conn)
    with refused(conn):
        conn.execute(
            text(
                "UPDATE campaigns SET stage = 'closed', closed_reason = 'cancelled', closed_at = now(), "
                "paused_at = now() WHERE id = :c"
            ),
            {"c": world.campaign},
        )


@pytest.mark.parametrize("address", ["no-at-sign", "a@b", "two words@lumen.example", " @lumen.example", "a@@b.example"])
def test_a_follow_up_address_without_a_name_an_at_sign_and_a_dotted_host_is_refused(conn, address):
    world = make_world(conn)
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE campaigns SET follow_up_email = :e WHERE id = :c"), {"e": address, "c": world.campaign}
        )
    assert "follow_up_email" in str(caught.value)


def test_a_typed_follow_up_address_is_accepted_and_one_can_be_cleared(conn):
    world = make_world(conn, follow_up_email="talent@lumen.example")
    assert one(conn, "SELECT follow_up_email FROM campaigns WHERE id = :c", c=world.campaign) == "talent@lumen.example"
    conn.execute(text("UPDATE campaigns SET follow_up_email = NULL WHERE id = :c"), {"c": world.campaign})
    with refused(conn):
        long_address = "a" * 250 + "@b.example"
        conn.execute(
            text("UPDATE campaigns SET follow_up_email = :e WHERE id = :c"), {"e": long_address, "c": world.campaign}
        )


def test_the_follow_up_wait_and_the_budget_have_limits(conn):
    world = make_world(conn)
    for column, value in (("follow_up_after_days", 0), ("follow_up_after_days", 61), ("budget_microusd", 0)):
        with refused(conn):
            conn.execute(text(f"UPDATE campaigns SET {column} = :v WHERE id = :c"), {"v": value, "c": world.campaign})


# ---- steps and dossiers ----------------------------------------------------------------------------------------------


def test_one_step_per_campaign_and_key(conn):
    world = make_world(conn)
    make_step(conn, world.campaign, "research")
    with refused(conn) as caught:
        make_step(conn, world.campaign, "research")
    assert "campaign_steps_once" in str(caught.value)
    assert make_step(conn, world.campaign, "fit", agent_role="analyst")


def test_a_step_cannot_name_another_persons_campaign(conn):
    world = make_world(conn)
    stranger = make_user(conn)
    with refused(conn):
        conn.execute(
            text(
                "INSERT INTO campaign_steps (campaign_id, user_id, step_key, agent_role) "
                "VALUES (:c, :u, 'research', 'researcher')"
            ),
            {"c": world.campaign, "u": stranger},
        )


def test_a_dossier_claim_must_carry_a_source_with_a_url(conn):
    world = make_world(conn)
    assert make_dossier(conn, world.campaign)
    bad_claims = (
        [{"text": "No sources at all.", "sources": []}],
        [{"text": "A source with no address.", "sources": [{"quote": "x"}]}],
        [{"text": "No sources key."}],
    )
    for version, claims in enumerate(bad_claims, start=2):
        with refused(conn):
            make_dossier(conn, world.campaign, claims=claims, version=version)
    with refused(conn):
        conn.execute(
            text("UPDATE company_dossiers SET claims = '{}'::jsonb WHERE campaign_id = :c"), {"c": world.campaign}
        )  # not a list


def test_one_dossier_version_per_campaign(conn):
    world = make_world(conn)
    make_dossier(conn, world.campaign)
    with refused(conn):
        make_dossier(conn, world.campaign)
    assert make_dossier(conn, world.campaign, version=2)


# ---- autonomy --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action_type", ["submit_application", "send_follow_up"])
def test_the_two_locked_action_types_refuse_tell_me_after(conn, action_type):
    user = make_user(conn)
    with refused(conn) as caught:
        conn.execute(
            text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, :t, 'tell_me_after')"),
            {"u": user, "t": action_type},
        )
    assert "autonomy_never_automatic" in str(caught.value)
    for level in ("ask_first", "never"):
        conn.execute(
            text(
                "INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, :t, CAST(:l AS autonomy_level)) "
                "ON CONFLICT (user_id, action_type) DO UPDATE SET level = EXCLUDED.level"
            ),
            {"u": user, "t": action_type, "l": level},
        )


def test_twin_answer_may_be_set_to_tell_me_after(conn):
    user = make_user(conn)
    conn.execute(
        text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'twin_answer', 'tell_me_after')"),
        {"u": user},
    )
    assert count(conn, "autonomy_settings WHERE user_id = :u", u=user) == 1


# ---- approvals -------------------------------------------------------------------------------------------------------


def make_proposed(conn: Connection, world: World, role: str = "operator") -> tuple[UUID, int]:
    run = make_run(conn, world.user, world.campaign, role)
    return run, make_action(conn, run, kind="outside_action", status="proposed")


def test_an_approval_is_decided_once(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    approval = make_approval(conn, action)
    decide = "UPDATE approvals SET status = :s, decided_at = now(), decided_via = 'app' WHERE id = :a"
    conn.execute(text(decide), {"s": "approved", "a": approval})
    for status in ("rejected", "approved"):
        with refused(conn) as caught:
            conn.execute(text(decide), {"s": status, "a": approval})
        assert "decided once" in str(caught.value)
    with refused(conn):
        conn.execute(text("DELETE FROM approvals WHERE id = :a"), {"a": approval})


def test_an_approval_cannot_be_given_after_it_expired(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    approval = make_approval(conn, action, expires_in="-1 minute")
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE approvals SET status = 'approved', decided_at = now(), decided_via = 'app' WHERE id = :a"),
            {"a": approval},
        )
    assert "expired" in str(caught.value)


def test_an_approval_is_decided_in_the_app_and_nowhere_else(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    approval = make_approval(conn, action)
    for via in ("email", "agent", "mcp"):
        with refused(conn):
            conn.execute(
                text("UPDATE approvals SET status = 'approved', decided_at = now(), decided_via = :v WHERE id = :a"),
                {"a": approval, "v": via},
            )
    with refused(conn):  # approved with no way of deciding recorded
        conn.execute(text("UPDATE approvals SET status = 'approved', decided_at = now() WHERE id = :a"), {"a": approval})


def test_one_approval_per_action(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    make_approval(conn, action)
    with refused(conn) as caught:
        make_approval(conn, action)
    assert "approvals_one_per_action" in str(caught.value)


def test_an_approval_cannot_name_another_persons_action(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    stranger = make_user(conn)
    with refused(conn):
        conn.execute(
            text("INSERT INTO approvals (user_id, agent_action_id, expires_at) VALUES (:u, :a, now() + interval '1 day')"),
            {"u": stranger, "a": action},
        )


# ---- guardian checks -------------------------------------------------------------------------------------------------


def test_guardian_checks_are_append_only_and_a_failure_needs_a_reason(conn):
    world = make_world(conn)
    _, action = make_proposed(conn, world)
    check = make_check(conn, action, "scope", True)
    for sql in ("UPDATE guardian_checks SET passed = false WHERE id = :i", "DELETE FROM guardian_checks WHERE id = :i"):
        with refused(conn) as caught:
            conn.execute(text(sql), {"i": check})
        assert "append-only" in str(caught.value)
    with refused(conn):
        conn.execute(
            text(
                "INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, guardian_version) "
                "VALUES (:a, :u, 'scope', false, 'test-1')"
            ),
            {"a": action, "u": world.user},
        )


# ---- the flight recorder's shape -------------------------------------------------------------------------------------


def test_an_outside_action_carries_a_type_a_policy_and_a_payload_and_a_step_none_of_them(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "guardian")
    assert make_action(conn, run, kind="state_change", status="recorded", outcome_code="paused")
    with refused(conn):  # an outside action with no payload
        conn.execute(
            text(
                "INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, action_type, status, summary, "
                "policy_decision, policy_version) VALUES (:r, :u, 90, 'outside_action', 'submit_application', "
                "'proposed', 's', 'ask', 'v')"
            ),
            {"r": run, "u": world.user},
        )
    with refused(conn):  # a model call that claims a type
        conn.execute(
            text(
                "INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, action_type, status, summary) "
                "VALUES (:r, :u, 91, 'model_call', 'submit_application', 'recorded', 's')"
            ),
            {"r": run, "u": world.user},
        )


def test_a_run_numbers_its_steps_once(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "researcher")
    make_action(conn, run, kind="tool_call", status="recorded")
    with refused(conn):
        conn.execute(
            text(
                "INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, status, summary) "
                "VALUES (:r, :u, 1, 'tool_call', 'recorded', 'again')"
            ),
            {"r": run, "u": world.user},
        )


# ---- record_state_change ---------------------------------------------------------------------------------------------


def state_changes(conn: Connection, campaign: UUID) -> list:
    return conn.execute(
        text(
            """SELECT a.id, a.seq, a.kind, a.status, a.outcome_code, a.summary, a.inputs, r.id AS run_id, r.agent_role
               FROM agent_actions a JOIN agent_runs r ON r.id = a.agent_run_id
               WHERE a.campaign_id = :c ORDER BY a.id"""
        ),
        {"c": campaign},
    ).all()


@pytest.mark.parametrize("role", ["app_api", "app_worker"])
def test_record_state_change_writes_a_state_change_row_under_one_control_run(conn, role):
    world = make_world(conn)
    act_as(conn, role, world.user)
    first = one(conn, "SELECT record_state_change(:c, 'paused by the candidate', 'paused', '{\"by\": \"candidate\"}')", c=world.campaign)
    second = one(conn, "SELECT record_state_change(:c, 'resumed by the candidate', 'resumed')", c=world.campaign)
    assert second > first
    back_to_bootstrap(conn)
    rows = state_changes(conn, world.campaign)
    assert [(r.seq, r.kind, r.status, r.outcome_code, r.agent_role) for r in rows] == [
        (1, "state_change", "recorded", "paused", "guardian"),
        (2, "state_change", "recorded", "resumed", "guardian"),
    ]
    assert rows[0].summary == "paused by the candidate" and rows[0].inputs == {"by": "candidate"}
    assert rows[0].run_id == rows[1].run_id
    assert count(conn, "agent_runs WHERE campaign_id = :c AND agent_role = 'guardian'", c=world.campaign) == 1


def test_record_state_change_reuses_the_control_run_that_exists(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "guardian")
    make_action(conn, run, kind="state_change", status="recorded")
    act_as(conn, "app_api", world.user)
    one(conn, "SELECT record_state_change(:c, 'cancelled', 'cancelled')", c=world.campaign)
    back_to_bootstrap(conn)
    assert count(conn, "agent_runs WHERE campaign_id = :c", c=world.campaign) == 1
    assert [r.seq for r in state_changes(conn, world.campaign)] == [1, 2]


def test_record_state_change_refuses_another_persons_campaign(conn):
    world = make_world(conn)
    stranger = make_user(conn)
    act_as(conn, "app_api", stranger)
    with refused(conn) as caught:
        conn.execute(text("SELECT record_state_change(:c, 'paused', 'paused')"), {"c": world.campaign})
    assert "not your campaign" in str(caught.value)
    act_as(conn, "app_api", None)  # no one signed in
    with refused(conn):
        conn.execute(text("SELECT record_state_change(:c, 'paused', 'paused')"), {"c": world.campaign})
    back_to_bootstrap(conn)
    assert state_changes(conn, world.campaign) == []


def test_record_state_change_is_not_callable_by_public(conn):
    world = make_world(conn)
    assert one(conn, "SELECT has_function_privilege('app_api', 'record_state_change(uuid, text, text, jsonb)', 'EXECUTE')")
    assert one(
        conn, "SELECT has_function_privilege('app_worker', 'record_state_change(uuid, text, text, jsonb)', 'EXECUTE')"
    )
    assert not one(
        conn, "SELECT has_function_privilege('app_match', 'record_state_change(uuid, text, text, jsonb)', 'EXECUTE')"
    )
    act_as(conn, "app_match", world.user)
    with refused(conn):
        conn.execute(text("SELECT record_state_change(:c, 'paused', 'paused')"), {"c": world.campaign})


def test_the_api_login_still_cannot_insert_an_action_or_a_check(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "operator")
    action = make_action(conn, run, kind="outside_action", status="proposed")
    act_as(conn, "app_api", world.user)
    with refused(conn) as caught:
        conn.execute(
            text(
                "INSERT INTO agent_actions (agent_run_id, user_id, seq, kind, status, summary) "
                "VALUES (:r, :u, 50, 'tool_call', 'recorded', 'forged')"
            ),
            {"r": run, "u": world.user},
        )
    assert "permission denied" in str(caught.value)
    with refused(conn) as caught:
        conn.execute(
            text(
                "INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, guardian_version) "
                "VALUES (:a, :u, 'scope', true, 'forged')"
            ),
            {"a": action, "u": world.user},
        )
    assert "permission denied" in str(caught.value)


# ---- row-level security and grants -----------------------------------------------------------------------------------


def make_job_id(conn: Connection) -> int:
    return make_job(
        conn, title="Other", description="Other.", requirements=[("Other", "required")], company_name="Other Demo"
    ).id


def seed_everything(conn: Connection, world: World) -> int:
    make_step(conn, world.campaign, "research")
    make_dossier(conn, world.campaign)
    run = make_run(conn, world.user, world.campaign, "operator")
    action = make_action(conn, run, kind="outside_action", status="proposed")
    make_approval(conn, action)
    make_check(conn, action, "scope", True)
    conn.execute(
        text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'twin_answer', 'never')"),
        {"u": world.user},
    )
    return action


def test_a_person_sees_only_their_own_rows_in_all_eight_tables(conn):
    mine, theirs = make_world(conn), make_world(conn)
    seed_everything(conn, mine)
    seed_everything(conn, theirs)
    act_as(conn, "app_api", mine.user)
    for table in CAMPAIGN_TABLES:
        assert count(conn, table) >= 1, table
        assert count(conn, f"{table} WHERE user_id <> :u", u=mine.user) == 0, table
    assert count(conn, "campaigns") == 1
    assert count(conn, "agent_actions") == 1
    back_to_bootstrap(conn)
    act_as(conn, "app_worker")
    assert count(conn, "campaigns") == 2  # the worker serves every person


def test_the_api_cannot_write_another_persons_rows(conn):
    mine, theirs = make_world(conn), make_world(conn)
    seed_everything(conn, theirs)
    other_job = make_job_id(conn)
    act_as(conn, "app_api", mine.user)
    result = conn.execute(text("UPDATE campaigns SET follow_up_email = 'x@y.example' WHERE id = :c"), {"c": theirs.campaign})
    assert result.rowcount == 0
    with refused(conn) as caught:  # a row for someone else fails the policy's WITH CHECK
        make_campaign(conn, theirs.user, other_job)
    assert "row-level security" in str(caught.value)


def test_the_api_may_update_actions_and_approvals_but_not_insert_them_or_delete_them(conn):
    world = make_world(conn)
    action = seed_everything(conn, world)
    act_as(conn, "app_api", world.user)
    assert conn.execute(text("UPDATE approvals SET note = NULL WHERE agent_action_id = :a"), {"a": action}).rowcount == 1
    with refused(conn):
        conn.execute(text("DELETE FROM agent_actions WHERE id = :a"), {"a": action})
    with refused(conn):
        conn.execute(text("DELETE FROM guardian_checks WHERE agent_action_id = :a"), {"a": action})


def test_the_worker_may_insert_actions_and_checks(conn):
    world = make_world(conn)
    run = make_run(conn, world.user, world.campaign, "operator")
    act_as(conn, "app_worker", world.user)
    action = one(
        conn,
        """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, status, summary)
           VALUES (:r, :u, :c, 1, 'tool_call', 'recorded', 'ok') RETURNING id""",
        r=run,
        u=world.user,
        c=world.campaign,
    )
    assert action
    outside = one(
        conn,
        """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, action_type, status, summary,
                                      payload, policy_decision, policy_version)
           VALUES (:r, :u, :c, 2, 'outside_action', 'send_follow_up', 'proposed', 's', '{}', 'ask', 'v') RETURNING id""",
        r=run,
        u=world.user,
        c=world.campaign,
    )
    conn.execute(
        text(
            "INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, guardian_version) "
            "VALUES (:a, :u, 'scope', true, 'v')"
        ),
        {"a": outside, "u": world.user},
    )


def test_the_match_login_reaches_none_of_the_eight_tables(conn):
    world = make_world(conn)
    seed_everything(conn, world)
    act_as(conn, "app_match", world.user)
    for table in CAMPAIGN_TABLES:
        with refused(conn):
            conn.execute(text(f"SELECT count(*) FROM {table}"))


def test_an_agent_token_request_sees_no_campaign_rows(conn):
    world = make_world(conn)
    seed_everything(conn, world)
    act_as(conn, "app_api", world.user)
    conn.execute(text("SELECT set_config('app.agent_token_id', :t, true)"), {"t": "00000000-0000-0000-0000-000000000001"})
    for table in CAMPAIGN_TABLES:
        assert count(conn, table) == 0, table


def test_erasing_the_campaign_takes_its_rows_with_it_but_the_recorder_needs_maintenance(conn):
    world = make_world(conn)
    seed_everything(conn, world)
    with refused(conn):  # the recorder is never emptied by a plain delete
        conn.execute(text("DELETE FROM agent_actions WHERE campaign_id = :c"), {"c": world.campaign})
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": world.user})
    for table in CAMPAIGN_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=world.user) == 0, table


