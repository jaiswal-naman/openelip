"""The wind-tunnel tables at the database (plan 08-01; DATA_MODEL 3.13, 4.5, 5.3, 5.7; EVL-03, EVL-04).

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Roles are taken on
with SET LOCAL ROLE when the test is about what that role may do. Every row is invented.
"""

import importlib
import json
from contextlib import contextmanager

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import make_world


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


def scenario(conn: Connection, key: str = "silent-employer", version: int = 1) -> object:
    return one(
        conn,
        """INSERT INTO sim_scenarios (key, version, persona, title, definition)
           VALUES (:k, :v, 'silent', 'An employer who never answers', CAST(:d AS jsonb)) RETURNING id""",
        k=key,
        v=version,
        d=json.dumps({"invented": True}),
    )


def eval_run(conn: Connection) -> object:
    return one(conn, "INSERT INTO eval_runs (suite, git_sha, config) VALUES ('wind_tunnel', 'abc', '{}') RETURNING id")


def attempt(conn: Connection, run, scenario_id, number: int, status: str = "passed", **extra) -> object:
    columns = {"eval_run_id": run, "scenario_id": scenario_id, "attempt": number, "seed": number, "status": status}
    columns.update(extra)
    names = ", ".join(columns)
    marks = ", ".join(f":{name}" if name != "status" else "CAST(:status AS sim_run_status)" for name in columns)
    return one(conn, f"INSERT INTO sim_runs ({names}) VALUES ({marks}) RETURNING id", **columns)


def result(conn: Connection, sim_run) -> int:
    return one(
        conn,
        "INSERT INTO sim_results (sim_run_id, kind, code, passed) "
        "VALUES (:r, 'outcome', 'goal_reached', true) RETURNING id",
        r=sim_run,
    )


def act_as(conn: Connection, role: str) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))


def verdict(conn: Connection, run, scenario_id):
    return conn.execute(
        text("SELECT attempts, attempts_passed, passed FROM sim_scenario_verdicts WHERE eval_run_id = :r"), {"r": run}
    ).one()


# ---- constraints and append-only -----------------------------------------------------------------------------------


def test_the_same_key_and_version_twice_is_refused_and_a_new_version_is_a_new_row(conn):
    scenario(conn)
    with refused(conn):
        scenario(conn)
    scenario(conn, version=2)


@pytest.mark.parametrize("number", [0, 6])
def test_an_attempt_outside_one_to_five_is_refused(conn, number):
    run, sid = eval_run(conn), scenario(conn)
    with refused(conn):
        attempt(conn, run, sid, number)


def test_a_second_row_for_one_run_scenario_and_attempt_is_refused(conn):
    run, sid = eval_run(conn), scenario(conn)
    attempt(conn, run, sid, 1)
    with refused(conn):
        attempt(conn, run, sid, 1)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sim_scenarios SET title = 'edited'",
        "DELETE FROM sim_scenarios",
        "UPDATE sim_results SET passed = false",
        "DELETE FROM sim_results",
    ],
)
def test_scenarios_and_results_cannot_be_changed_or_removed(conn, sql):
    run, sid = eval_run(conn), scenario(conn)
    result(conn, attempt(conn, run, sid, 1))
    with refused(conn):
        conn.execute(text(sql))


# ---- the verdict view ----------------------------------------------------------------------------------------------


def test_five_passed_attempts_pass_the_scenario(conn):
    run, sid = eval_run(conn), scenario(conn)
    for number in range(1, 6):
        attempt(conn, run, sid, number)
    assert tuple(verdict(conn, run, sid)) == (5, 5, True)


@pytest.mark.parametrize(
    ("statuses", "counts"),
    [
        (["passed"] * 4 + ["failed"], (5, 4)),
        (["passed"] * 4, (4, 4)),
        (["passed"] * 4 + ["error"], (5, 4)),
        (["passed"] * 4 + ["running"], (5, 4)),
    ],
)
def test_anything_short_of_five_passing_attempts_fails_the_scenario(conn, statuses, counts):
    run, sid = eval_run(conn), scenario(conn)
    for number, status in enumerate(statuses, start=1):
        attempt(conn, run, sid, number, status)
    row = verdict(conn, run, sid)
    assert (row.attempts, row.attempts_passed, row.passed) == (*counts, False)


# ---- roles, row-level security and the view ------------------------------------------------------------------------


def test_row_level_security_is_on_and_the_view_runs_with_the_callers_rights(conn):
    flags = conn.execute(
        text(
            "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN ('sim_scenarios', 'sim_runs', 'sim_results')"
        )
    ).all()
    assert len(flags) == 3 and all(flag for _, flag in flags)
    options = one(conn, "SELECT reloptions FROM pg_class WHERE relname = 'sim_scenario_verdicts'")
    assert "security_invoker=true" in options


@pytest.mark.parametrize("role", ["app_api", "app_match"])
def test_the_api_and_match_logins_read_everything_and_write_nothing(conn, role):
    run, sid = eval_run(conn), scenario(conn)
    sim = attempt(conn, run, sid, 1)
    result(conn, sim)
    act_as(conn, role)
    for table in ("sim_scenarios", "sim_runs", "sim_results", "sim_scenario_verdicts"):
        assert one(conn, f"SELECT count(*) FROM {table}") == 1
    for sql in (
        "INSERT INTO sim_scenarios (key, version, persona, title, definition) VALUES ('x', 1, 'silent', 't', '{}')",
        "INSERT INTO sim_results (sim_run_id, kind, code, passed) VALUES (gen_random_uuid(), 'outcome', 'x', true)",
        "UPDATE sim_runs SET status = 'failed'",
        "DELETE FROM sim_runs",
    ):
        with refused(conn) as caught:
            conn.execute(text(sql))
        assert "permission denied" in str(caught.value)


def test_the_worker_writes_scenarios_results_and_attempts_but_changes_only_attempts(conn):
    run = eval_run(conn)
    act_as(conn, "app_worker")
    sid = scenario(conn)
    sim = attempt(conn, run, sid, 1, "running")
    result(conn, sim)
    conn.execute(text("UPDATE sim_runs SET status = 'passed', finished_at = now() WHERE id = :i"), {"i": sim})
    for sql in ("UPDATE sim_scenarios SET title = 'x'", "UPDATE sim_results SET passed = false"):
        with refused(conn) as caught:
            conn.execute(text(sql))
        assert "permission denied" in str(caught.value)
    assert one(conn, "SELECT count(*) FROM sim_scenario_verdicts") == 1


# ---- erasure -------------------------------------------------------------------------------------------------------


def test_deleting_the_synthetic_user_or_the_campaign_keeps_the_attempt_and_clears_the_reference(conn):
    world = make_world(conn)
    run, sid = eval_run(conn), scenario(conn)
    sim = attempt(conn, run, sid, 1, candidate_user_id=world.user, campaign_id=world.campaign)
    conn.execute(text("SET LOCAL app.maintenance = 'on'"))  # erase_user's own flag: it lets the agent rows go
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": world.user})
    row = conn.execute(text("SELECT candidate_user_id, campaign_id FROM sim_runs WHERE id = :i"), {"i": sim}).one()
    assert (row.candidate_user_id, row.campaign_id) == (None, None)


def test_the_three_tables_are_registered_once_by_the_two_composition_roots(conn):
    from app.trust import privacy

    importlib.import_module("app.main")
    importlib.import_module("app.worker")
    registered = {table: effect for tables in privacy.registry().values() for table, effect in tables.items()}
    assert registered["sim_runs"] == "set_null"
    assert registered["sim_scenarios"] == registered["sim_results"] == "no_personal_data"
