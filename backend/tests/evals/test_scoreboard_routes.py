"""08-03: GET /v1/control/evals and /v1/control/evals/runs/{id}. Runs, scenarios and results are invented and inserted
directly (the wind tunnel itself is 08-04), then read through the real app on the API login."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.evals import routes
from app.main import create_app
from app.platform import config, db
from app.platform.sessions import Principal, current_user
from tests.conftest import _empty_tables
from tests.hiring.factories import make_org
from tests.match.factories import make_user

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def client(user: UUID | None) -> TestClient:
    app = create_app()
    if user:
        app.dependency_overrides[current_user] = lambda: Principal(user)
    return TestClient(app)


def board(user):
    response = client(user).get("/v1/control/evals")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def add_run(engine, suite, *, status="passed", hours=0, config=None, metrics=None, sha="abc1234") -> UUID:
    cfg = {"mode": "replay"} if config is None else config
    finished = None if status == "running" else T0 + timedelta(hours=hours)
    if metrics is None and status != "running":
        metrics = {"n": 4, "passed": 3, "score": 0.75}
    with engine.begin() as conn:
        return conn.execute(
            text(
                """INSERT INTO eval_runs (suite, git_sha, config, status, metrics, started_at, finished_at)
                   VALUES (CAST(:suite AS eval_suite), :sha, CAST(:cfg AS jsonb), CAST(:status AS eval_status),
                           CAST(:metrics AS jsonb), :started, :finished) RETURNING id"""
            ),
            {
                "suite": suite,
                "sha": sha,
                "cfg": json.dumps(cfg),
                "status": status,
                "metrics": None if metrics is None else json.dumps(metrics),
                "started": T0 + timedelta(hours=hours) - timedelta(minutes=5),
                "finished": finished,
            },
        ).scalar_one()


def add_scenario(engine, key, persona, *, version=1, title=None) -> UUID:
    with engine.begin() as conn:
        return conn.execute(
            text(
                """INSERT INTO sim_scenarios (key, version, persona, title, definition)
                   VALUES (:k, :v, CAST(:p AS sim_persona), :t, '{}') RETURNING id"""
            ),
            {"k": key, "v": version, "p": persona, "t": title or f"Invented {key}"},
        ).scalar_one()


def add_attempts(engine, run, scenario, statuses, *, results=None) -> list[UUID]:
    ids = []
    with engine.begin() as conn:
        for n, status in enumerate(statuses, start=1):
            sim = conn.execute(
                text(
                    """INSERT INTO sim_runs (eval_run_id, scenario_id, attempt, seed, status, started_at, finished_at)
                       VALUES (:e, :s, :a, :seed, CAST(:st AS sim_run_status), :t, :t) RETURNING id"""
                ),
                {"e": run, "s": scenario, "a": n, "seed": 1000 + n, "st": status, "t": T0},
            ).scalar_one()
            ids.append(sim)
            for kind, code, passed, details in results or [("outcome", "goal_reached", status == "passed", {})]:
                conn.execute(
                    text(
                        """INSERT INTO sim_results (sim_run_id, kind, code, passed, details)
                           VALUES (:r, CAST(:k AS sim_result_kind), :c, :p, CAST(:d AS jsonb))"""
                    ),
                    {"r": sim, "k": kind, "c": code, "p": passed, "d": json.dumps(details)},
                )
    return ids


def candidate(engine):
    with engine.begin() as conn:
        return make_user(conn)


def tunnel_metrics(**scenarios):
    return {
        "n": len(scenarios),
        "passed": 0,
        "score": 0.0,
        "scenarios": scenarios,
        "human_minutes_per_interview": 6.5,
    }


# ---- who may read ----------------------------------------


def test_signed_out_is_401(clean):
    assert client(None).get("/v1/control/evals").status_code == 401
    assert client(None).get(f"/v1/control/evals/runs/{uuid4()}").status_code == 401


def test_a_candidate_and_an_employer_only_account_both_read_the_same_board(clean):
    person = candidate(clean)
    with clean.begin() as conn:
        employer = make_org(conn).owner
    add_run(clean, "guardian")
    assert board(person) == board(employer)
    assert board(person)["suites"]


# ---- suites ----------------------------------------


def test_with_nothing_recorded_every_suite_is_null_never_zero(clean):
    person = candidate(clean)
    data = board(person)
    names = [s["suite"] for s in data["suites"]]
    with clean.connect() as conn:  # every eval_suite value but the two that are not suites of a gold set
        enum = set(conn.execute(text("SELECT unnest(enum_range(NULL::eval_suite))::text")).scalars())
    assert set(names) == enum - {"wind_tunnel", "harness_smoke"} and len(names) == len(enum) - 2
    assert "wind_tunnel" not in names and "harness_smoke" not in names
    assert all(s["result"] is None for s in data["suites"])
    assert data["wind_tunnel"]["last_live_run"] is None
    assert data["wind_tunnel"]["human_minutes_per_interview"] is None
    assert [p["persona"] for p in data["wind_tunnel"]["personas"]] == [
        "silent", "lowball", "scam", "hidden_instruction", "cooperative",
    ]  # fmt: skip
    assert all(p["scenarios"] == [] for p in data["wind_tunnel"]["personas"])


def test_a_suite_shows_its_latest_finished_run_with_where_it_came_from(clean):
    person = candidate(clean)
    add_run(clean, "guardian", status="failed", hours=1, sha="old0001", metrics={"n": 10, "passed": 9, "score": 0.9})
    newest = add_run(
        clean,
        "guardian",
        status="passed",
        hours=2,
        sha="new0002",
        config={"mode": "live", "model": "invented/model-1", "labels": "agent-drafted, maintainer review pending"},
        metrics={"n": 20, "passed": 20, "score": 1.0},
    )
    add_run(clean, "guardian", status="running", hours=3)  # still running: skipped
    add_run(clean, "guardian", status="error", hours=4, metrics={"error_code": "boom"})  # ended in error: skipped
    result = {s["suite"]: s["result"] for s in board(person)["suites"]}["guardian"]
    assert result == {
        "run_id": str(newest),
        "status": "passed",
        "mode": "live",
        "n": 20,
        "passed": 20,
        "score": 1.0,
        "git_sha": "new0002",
        "finished_at": "2026-11-20T14:00:00Z",
        "model": "invented/model-1",
        "labels": "agent-drafted, maintainer review pending",
    }


def test_mode_is_replay_when_the_config_does_not_say_and_a_zero_is_shown_only_when_recorded(clean):
    person = candidate(clean)
    add_run(clean, "chat", config={}, metrics={"n": 5, "passed": 0, "score": 0.0})
    add_run(clean, "dossier", config={"mode": "replay"}, metrics={"harness_check_only": True})
    got = {s["suite"]: s["result"] for s in board(person)["suites"]}
    assert (got["chat"]["mode"], got["chat"]["passed"], got["chat"]["score"]) == ("replay", 0, 0.0)  # recorded zero
    assert (got["dossier"]["n"], got["dossier"]["passed"], got["dossier"]["score"]) == (None, None, None)
    assert got["injection"] is None


def test_harness_smoke_runs_are_not_on_the_board(clean):
    person = candidate(clean)
    add_run(clean, "harness_smoke")
    assert "harness_smoke" not in [s["suite"] for s in board(person)["suites"]]


# ---- the wind tunnel ----------------------------------------


def test_a_four_of_five_scenario_does_not_pass_and_shows_the_failing_attempt(clean):
    person = candidate(clean)
    run = add_run(
        clean,
        "wind_tunnel",
        status="failed",
        config={"mode": "replay", "scenario_versions": {"silent-1": 1}},
        metrics=tunnel_metrics(
            **{
                "silent-1": {
                    "persona": "silent",
                    "version": 1,
                    "attempts_passed": 4,
                    "passed": False,
                    "medians": {"human_minutes_per_interview": None, "cost_per_campaign_usd": 0.12, "agent_steps": 14},
                }
            }
        ),
        sha="tun0001",
    )
    scenario = add_scenario(clean, "silent-1", "silent", title="Invented silent employer")
    add_attempts(clean, run, scenario, ["passed", "passed", "failed", "passed", "passed"])
    silent = next(p for p in board(person)["wind_tunnel"]["personas"] if p["persona"] == "silent")
    assert silent["scenarios"] == [
        {
            "key": "silent-1",
            "title": "Invented silent employer",
            "version": 1,
            "result": {
                "run_id": str(run),
                "version": 1,
                "attempts": 5,
                "attempts_passed": 4,
                "passed": False,
                "attempt_statuses": ["passed", "passed", "failed", "passed", "passed"],
                "medians": {"human_minutes_per_interview": None, "cost_per_campaign_usd": 0.12, "agent_steps": 14},
                "mode": "replay",
                "git_sha": "tun0001",
                "finished_at": "2026-11-20T12:00:00Z",
            },
        }
    ]


def test_five_of_five_passes_and_a_scenario_no_run_included_has_no_result(clean):
    person = candidate(clean)
    run = add_run(clean, "wind_tunnel", metrics=tunnel_metrics(**{"cooperative-1": {"medians": {}}}))
    add_attempts(clean, run, add_scenario(clean, "cooperative-1", "cooperative"), ["passed"] * 5)
    add_scenario(clean, "lowball-1", "lowball")  # stored, never run
    personas = {p["persona"]: p["scenarios"] for p in board(person)["wind_tunnel"]["personas"]}
    assert personas["cooperative"][0]["result"]["passed"] is True
    assert personas["lowball"][0]["result"] is None
    assert personas["scam"] == []


def test_five_attempts_with_one_error_fail_and_a_run_that_is_still_running_is_ignored(clean):
    person = candidate(clean)
    done = add_run(clean, "wind_tunnel", hours=1)
    running = add_run(clean, "wind_tunnel", status="running", hours=2)
    scenario = add_scenario(clean, "scam-1", "scam")
    add_attempts(clean, done, scenario, ["passed", "passed", "passed", "passed", "error"])
    add_attempts(clean, running, scenario, ["passed"])
    result = board(person)["wind_tunnel"]["personas"][2]["scenarios"][0]["result"]
    assert (result["run_id"], result["passed"], result["attempts_passed"]) == (str(done), False, 4)
    assert result["attempt_statuses"][-1] == "error"
    assert result["medians"] is None  # this run recorded none: null, not an invented value


def test_the_latest_version_of_a_scenario_is_listed_and_the_result_names_the_version_run(clean):
    person = candidate(clean)
    run = add_run(clean, "wind_tunnel")
    v1 = add_scenario(clean, "silent-1", "silent", version=1, title="First wording")
    add_scenario(clean, "silent-1", "silent", version=2, title="Second wording")
    add_attempts(clean, run, v1, ["passed"] * 5)
    scenario = board(person)["wind_tunnel"]["personas"][0]["scenarios"][0]
    assert (scenario["title"], scenario["version"]) == ("Second wording", 2)
    assert scenario["result"]["version"] == 1  # the recorded run used version 1: the board says so


def test_last_live_run_and_the_human_minutes_proxy_come_from_stored_runs(clean):
    person = candidate(clean)
    live = add_run(clean, "wind_tunnel", hours=1, config={"mode": "live"}, metrics=tunnel_metrics())
    newest = add_run(clean, "wind_tunnel", hours=2, metrics=tunnel_metrics())
    tunnel = board(person)["wind_tunnel"]
    assert tunnel["last_live_run"] == {"run_id": str(live), "finished_at": "2026-11-20T13:00:00Z"}
    assert tunnel["human_minutes_per_interview"] == {"value": 6.5, "run_id": str(newest), "label": "a proxy"}


def test_a_null_human_minutes_proxy_stays_null(clean):
    person = candidate(clean)
    add_run(clean, "wind_tunnel", metrics={**tunnel_metrics(), "human_minutes_per_interview": None})
    assert board(person)["wind_tunnel"]["human_minutes_per_interview"] is None


# ---- run detail ----------------------------------------


def test_detail_reduces_the_config_to_the_allow_list_and_lists_every_attempt(clean):
    person = candidate(clean)
    run = add_run(
        clean,
        "wind_tunnel",
        status="failed",
        config={
            "mode": "replay",
            "scenario_versions": {"scam-1": 1},
            "attempts": 5,
            "labels": "agent-drafted, maintainer review pending",
            "database_url": "postgresql://secret",
            "worker": "windtunnel:host:1",
        },
        metrics=tunnel_metrics(),
    )
    scenario = add_scenario(clean, "scam-1", "scam")
    add_attempts(
        clean,
        run,
        scenario,
        ["passed", "failed"],
        results=[
            ("violation", "invented_claim", True, {"count": 0, "refs": []}),
            ("outcome", "goal_reached", False, {}),
        ],
    )
    response = client(person).get(f"/v1/control/evals/runs/{run}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["config"] == {
        "mode": "replay",
        "scenario_versions": {"scam-1": 1},
        "attempts": 5,
        "labels": "agent-drafted, maintainer review pending",
    }
    assert "secret" not in response.text and "windtunnel:host" not in response.text
    assert (data["suite"], data["status"], data["mode"], data["git_sha"]) == (
        "wind_tunnel",
        "failed",
        "replay",
        "abc1234",
    )
    assert [(a["scenario_key"], a["attempt"], a["seed"], a["status"]) for a in data["attempts"]] == [
        ("scam-1", 1, 1001, "passed"), ("scam-1", 2, 1002, "failed"),
    ]  # fmt: skip
    assert data["attempts"][1]["results"] == [
        {"kind": "violation", "code": "invented_claim", "passed": True, "details": {"count": 0, "refs": []}},
        {"kind": "outcome", "code": "goal_reached", "passed": False, "details": {}},
    ]
    assert data["metrics"]["human_minutes_per_interview"] == 6.5


def test_detail_of_a_suite_run_has_no_attempts_and_no_items(clean):
    person = candidate(clean)
    run = add_run(clean, "guardian", config={"mode": "replay", "task": "guardian.check"})
    with clean.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO eval_items (eval_run_id, item_key, expected, actual, is_passed)
                   VALUES (:r, 'item-secret-key', '{"v": "expected-secret"}', '{"v": "actual-secret"}', true)"""
            ),
            {"r": run},
        )
    response = client(person).get(f"/v1/control/evals/runs/{run}")
    data = response.json()["data"]
    assert data["attempts"] is None and data["config"] == {"mode": "replay"}
    assert "secret" not in response.text


def test_harness_smoke_and_unknown_runs_are_404(clean):
    person = candidate(clean)
    smoke = add_run(clean, "harness_smoke")
    for target in (smoke, uuid4()):
        response = client(person).get(f"/v1/control/evals/runs/{target}")
        assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


def test_the_scoreboard_never_reads_eval_items():
    import inspect

    assert "eval_items" not in inspect.getsource(routes).replace("`eval_items`", "").replace("No `eval_items` is", "")


def test_the_handlers_are_plain_def():
    import inspect

    assert not inspect.iscoroutinefunction(routes.scoreboard)
    assert not inspect.iscoroutinefunction(routes.run_detail)


def test_the_scoreboard_needs_only_the_api_login(clean, monkeypatch):
    person = candidate(clean)
    run = add_run(clean, "wind_tunnel")
    add_attempts(clean, run, add_scenario(clean, "scam-1", "scam"), ["passed"] * 5)
    changed = replace(config.get(), database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})
    try:
        assert board(person)["wind_tunnel"]["personas"][2]["scenarios"][0]["result"]["passed"] is True
        assert client(person).get(f"/v1/control/evals/runs/{run}").status_code == 200
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
