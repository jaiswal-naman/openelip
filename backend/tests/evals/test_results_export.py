"""EVL-05: a result leaves the database only as an export of a finished recorded run.

Runs are invented and inserted directly. Every refusal leaves the output folder as it was (no folder, no file).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.evals import results
from app.evals.__main__ import main
from tests.conftest import _empty_tables

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
METRICS = {"n": 10, "passed": 10, "score": 1.0, "attempts": 50, "scenarios": {"a-key": {"passed": True}}}


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def add_run(engine, suite="wind_tunnel", *, status="passed", config=None, metrics=METRICS, sha="abc1234") -> uuid.UUID:
    finished = None if status == "running" else T0
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
                "cfg": json.dumps(config if config is not None else {"mode": "replay", "labels": "agent-drafted"}),
                "status": status,
                "metrics": None if status == "running" else json.dumps(metrics),
                "started": T0 - timedelta(minutes=5),
                "finished": finished,
            },
        ).scalar_one()


def test_a_finished_replay_run_is_written_with_its_run_id_commit_and_the_wiring_check_note(clean, tmp_path):
    run = add_run(clean)
    path = results.export(run, "wind_tunnel", tmp_path / "out")
    assert path == tmp_path / "out" / "wind_tunnel.json"
    text_ = path.read_text(encoding="utf-8")
    assert text_.endswith("}\n") and text_ == json.dumps(json.loads(text_), indent=2, sort_keys=True) + "\n"
    assert json.loads(text_) == {
        "format": 1,
        "suite": "wind_tunnel",
        "run_id": str(run),
        "status": "passed",
        "mode": "replay",
        "git_sha": "abc1234",
        "started_at": (T0 - timedelta(minutes=5)).isoformat(),
        "finished_at": T0.isoformat(),
        "n": 10,
        "passed": 10,
        "score": 1.0,
        "metrics": METRICS,
        "labels": "agent-drafted",
        "note": "Replay of recorded model answers: a wiring check, not a quality measure.",
    }
    assert "a wiring check" in results.REPLAY_NOTE


def test_a_failed_run_is_exported_as_failed_and_a_live_run_has_no_replay_note(clean, tmp_path):
    failed = add_run(clean, status="failed", config={"mode": "replay"}, metrics={"n": 10, "passed": 9, "score": 0.0})
    live = add_run(clean, config={"mode": "live"})
    one = json.loads(results.export(failed, "wind_tunnel", tmp_path / "a").read_text())
    other = json.loads(results.export(live, "wind_tunnel", tmp_path / "b").read_text())
    assert one["status"] == "failed" and one["score"] == 0.0 and one["labels"] is None
    assert other["mode"] == "live" and "wiring check" not in other["note"]


def test_the_command_exports_to_the_given_folder_and_prints_the_path(clean, tmp_path, capsys):
    run = add_run(clean)
    assert main(["export", "wind_tunnel", "--run", str(run), "--out", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == str(tmp_path / "wind_tunnel.json")
    assert json.loads((tmp_path / "wind_tunnel.json").read_text())["run_id"] == str(run)


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("harness", "harness check"),
        ("unknown", "does not exist"),
        ("other_suite", "belongs to suite guardian"),
        ("running", "status running"),
        ("error", "status error"),
    ],
)
def test_a_run_that_may_not_leave_is_refused_and_nothing_is_written(clean, tmp_path, capsys, case, why):
    out = tmp_path / "out"
    suite, run = "wind_tunnel", uuid.uuid4()
    if case == "harness":
        suite, run = "harness_smoke", add_run(clean, "harness_smoke")
    elif case == "other_suite":
        run = add_run(clean, "guardian")
    elif case == "running":
        run = add_run(clean, status="running")
    elif case == "error":
        run = add_run(clean, status="error", metrics={"error_code": "ValueError"})
    with pytest.raises(results.ExportRefused, match=why):
        results.export(run, suite, out)
    assert not out.exists()
    assert main(["export", suite, "--run", str(run), "--out", str(out)]) == 2
    assert why in capsys.readouterr().err and not out.exists()
