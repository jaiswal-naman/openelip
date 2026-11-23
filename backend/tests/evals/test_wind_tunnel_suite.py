"""EVL-03, EVL-04: `wind_tunnel` is a gated suite. All ten scenarios run five times each on recorded answers, the run
is gated green, a degraded fixture is gated red, and the scoreboard shows every scenario with that run's id.

The commands are called in process (`main([...])`) on the shared test database with the application's own settings (the
tunnel needs them, and the logins the root conftest forces), after truncating `DATA_TABLES` (decision 37). The full run
waits on real undo windows, so it takes several minutes.
"""

import os
import uuid
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.evals import EVALS, gate, runner
from app.evals.__main__ import main
from app.evals.windtunnel import run
from app.main import create_app
from app.platform import config
from app.platform.llm import fake
from app.platform.sessions import Principal, current_user
from tests.conftest import _empty_tables
from tests.match.factories import make_user

FIXTURE = EVALS / "wind_tunnel" / "fixtures" / "degraded"
TEN = {
    "cooperative": ["cooperative-introduction", "cooperative-match"],
    "silent": ["silent-introduction", "silent-match"],
    "lowball": ["lowball-introduction", "lowball-match"],
    "scam": ["scam-introduction", "scam-match"],
    "hidden_instruction": ["hidden-instruction-introduction", "hidden-instruction-match"],
}


@pytest.fixture(scope="module")
def engine(database_url: URL) -> Iterator[Engine]:
    eng = create_engine(database_url.set(drivername="postgresql+psycopg"))
    _empty_tables(eng)
    yield eng
    _empty_tables(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def _fake_is_clean() -> Iterator[None]:
    fake.replay_from(None)
    yield
    fake.replay_from(None)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch):
    """Choose LLM_PROVIDER for the test; the settings cache follows."""

    def choose(name: str) -> None:
        monkeypatch.setenv("LLM_PROVIDER", name)
        config.get.cache_clear()

    yield choose
    monkeypatch.undo()
    config.get.cache_clear()


def last_line(text_: str) -> str:
    return [line for line in text_.splitlines() if line.strip()][-1].strip()


# ---- the commands, without a run ---------------------------------------------------------------------------------


def test_live_keeps_the_configured_provider_and_reaches_the_run_with_live_set(provider, monkeypatch, capsys):
    provider("anthropic")
    seen = {}

    def stub(keys, **kwargs):
        seen.update(keys=keys, provider=os.environ["LLM_PROVIDER"], **kwargs)
        return runner.Finished(uuid.uuid4(), "passed", 1, 1)

    monkeypatch.setattr(run, "run", stub)
    assert main(["windtunnel", "cooperative-match", "--live", "--pace", "0.5"]) == 0
    assert seen["keys"] == ["cooperative-match"] and seen["live"] is True and seen["pace"] == 0.5
    assert seen["provider"] == "anthropic" and seen["attempts"] == 5
    assert os.environ["LLM_PROVIDER"] == "anthropic"  # given back when the command ends


def test_every_other_command_forces_the_fake_provider(provider, monkeypatch):
    provider("anthropic")
    seen = {}

    def stub(name, recorded=None, *, live=False):
        seen["provider"] = os.environ["LLM_PROVIDER"]
        return runner.Finished(uuid.uuid4(), "passed", 5, 5)

    monkeypatch.setattr(runner, "run_suite", stub)
    assert main(["run", "harness_smoke"]) == 0
    assert seen["provider"] == "fake" and os.environ["LLM_PROVIDER"] == "anthropic"

    def stub_tunnel(keys, **kwargs):
        seen["tunnel"] = (os.environ["LLM_PROVIDER"], kwargs["live"])
        return runner.Finished(uuid.uuid4(), "passed", 1, 1)

    monkeypatch.setattr(run, "run", stub_tunnel)
    assert main(["windtunnel", "cooperative-match"]) == 0
    assert seen["tunnel"] == ("fake", False)
    assert main(["run", "wind_tunnel"]) == 0
    assert seen["tunnel"] == ("fake", False)


def test_live_with_the_fake_provider_exits_2_and_writes_nothing(engine, provider, capsys):
    provider("fake")
    assert main(["windtunnel", "cooperative-match", "--live"]) == 2
    assert "not fake" in capsys.readouterr().err
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eval_runs")).scalar_one() == 0


def test_an_unknown_scenario_exits_2_and_names_the_known_keys(engine, provider, capsys):
    provider("fake")
    assert main(["windtunnel", "no-such-scenario"]) == 2
    err = capsys.readouterr().err
    assert "no-such-scenario" in err and "cooperative-match" in err and "silent-match" in err
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eval_runs")).scalar_one() == 0


def test_the_wind_tunnel_cannot_be_run_live_or_recorded_through_run(provider, capsys):
    provider("fake")
    for extra in (["--live"], ["--recorded", "x.json"]):
        with pytest.raises(SystemExit) as refused:
            main(["run", "wind_tunnel", *extra])
        assert refused.value.code == 2


def test_the_floor_is_one_and_the_scenarios_are_the_ten():
    assert gate.floor("wind_tunnel") == 1.0
    from app.evals.windtunnel import scenario

    found = scenario.load_dir(run.SCENARIOS)
    assert {s.key for s in found} == {key for keys in TEN.values() for key in keys}
    assert {persona: sorted(s.key for s in found if s.persona == persona) for persona in TEN} == TEN
    assert {s.start for s in found if s.key.endswith("-match")} == {"match"}
    assert {s.start for s in found if s.key.endswith("-introduction")} == {"introduction"}
    assert {s.version for s in found} == {1}


# ---- the suite ---------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def suite(engine, database_url):
    """`run wind_tunnel` once: its exit code, its run id (the last line) and what the scoreboard shows."""
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        patch.setenv("LLM_PROVIDER", "fake")
        config.get.cache_clear()
        _empty_tables(engine)
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["run", "wind_tunnel"])
        yield {"code": code, "run_id": UUID(last_line(out.getvalue())), "stdout": out.getvalue()}
    config.get.cache_clear()


def test_run_wind_tunnel_runs_ten_scenarios_five_times_each_and_records_the_run(engine, suite):
    assert suite["code"] == 0
    with engine.connect() as conn:
        run_ = conn.execute(
            text("SELECT suite::text AS suite, status::text AS status, config, metrics FROM eval_runs WHERE id = :i"),
            {"i": suite["run_id"]},
        ).one()
        attempts = conn.execute(
            text(
                """SELECT s.key, count(*) AS n, count(*) FILTER (WHERE r.status = 'passed') AS won
                     FROM sim_runs r JOIN sim_scenarios s ON s.id = r.scenario_id GROUP BY s.key"""
            )
        ).all()
    assert run_.suite == "wind_tunnel" and run_.status == "passed"
    assert run_.config["mode"] == "replay" and run_.config["attempts"] == 5
    assert run_.metrics["n"] == 10 and run_.metrics["passed"] == 10 and run_.metrics["score"] == 1.0
    assert run_.metrics["attempts"] == 50 and run_.metrics["attempts_passed"] == 50
    assert set(run_.metrics["totals"].values()) == {0}
    assert {(a.key, a.n, a.won) for a in attempts} == {(key, 5, 5) for keys in TEN.values() for key in keys}


def test_the_gate_passes_the_run_and_prints_the_scenarios_passed(suite, capsys):
    assert main(["gate", "wind_tunnel", "--run", str(suite["run_id"])]) == 0
    assert "10 of 10" in capsys.readouterr().out


def test_the_scoreboard_lists_all_ten_scenarios_with_that_run_for_a_fresh_account(engine, suite):
    with engine.begin() as conn:
        user = make_user(conn)
    app = create_app()
    app.dependency_overrides[current_user] = lambda: Principal(user)
    response = TestClient(app).get("/v1/control/evals")
    assert response.status_code == 200, response.text
    personas = response.json()["data"]["wind_tunnel"]["personas"]
    listed = {
        persona["persona"]: sorted(
            (s["key"], s["result"]["run_id"], s["result"]["passed"], s["result"]["attempts_passed"])
            for s in persona["scenarios"]
        )
        for persona in personas
    }
    run_id = str(suite["run_id"])
    assert listed == {persona: sorted((key, run_id, True, 5) for key in keys) for persona, keys in TEN.items()}


def test_the_export_of_the_run_is_the_results_file(suite, tmp_path, capsys):
    assert main(["export", "wind_tunnel", "--run", str(suite["run_id"]), "--out", str(tmp_path)]) == 0
    import json

    document = json.loads((tmp_path / "wind_tunnel.json").read_text())
    assert document["run_id"] == str(suite["run_id"]) and document["n"] == 10 and document["score"] == 1.0
    assert document["mode"] == "replay" and "wiring check" in document["note"]


def test_a_degraded_scenario_gates_red(engine, suite, provider, capsys):
    """The fixture expects a follow-up that an introduction campaign never sends: its run must fail the gate."""
    provider("fake")
    capsys.readouterr()
    code = main(["windtunnel", "degraded-introduction", "--scenarios-dir", str(FIXTURE)])
    out = capsys.readouterr().out
    assert code == 0
    degraded = UUID(last_line(out))
    assert degraded != suite["run_id"]
    assert main(["gate", "wind_tunnel", "--run", str(degraded)]) == 1
    assert "FAIL" in capsys.readouterr().out
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status::text AS status, metrics FROM eval_runs WHERE id = :i"), {"i": degraded}
        ).one()
        won = conn.execute(
            text(
                """SELECT count(*) FROM sim_results x JOIN sim_runs r ON r.id = x.sim_run_id
                    WHERE r.eval_run_id = :i AND x.code = 'follow_ups_sent' AND NOT x.passed"""
            ),
            {"i": degraded},
        ).scalar_one()
    assert row.status == "failed" and row.metrics["score"] == 0.0 and won == 5


def test_a_run_with_an_attempt_in_error_gates_red(engine, suite, provider, capsys, tmp_path):
    """Remove a recording the scenario needs: its attempts end in error and the run cannot pass."""
    import shutil

    provider("fake")
    shutil.copytree(FIXTURE / "degraded-introduction", tmp_path / "errored-introduction")
    (tmp_path / "errored-introduction" / "recorded" / "research.plan.json").unlink()
    text_ = (FIXTURE / "degraded-introduction.toml").read_text()
    text_ = text_.replace("degraded-introduction", "errored-introduction").replace(
        "follow_ups_sent = 1", "follow_ups_sent = 0"
    )
    (tmp_path / "errored-introduction.toml").write_text(text_)
    capsys.readouterr()
    assert main(["windtunnel", "errored-introduction", "--scenarios-dir", str(tmp_path)]) == 0
    errored = UUID(last_line(capsys.readouterr().out))
    assert main(["gate", "wind_tunnel", "--run", str(errored)]) == 1
    with engine.connect() as conn:
        statuses = (
            conn.execute(text("SELECT status::text FROM sim_runs WHERE eval_run_id = :i"), {"i": errored})
            .scalars()
            .all()
        )
    assert statuses == ["error"] * 5
