"""PLT-08: the eval harness records a run and the gate judges it from the record.

Each test is one behaviour from plan 01-06 task 1. The gate must pass the shipped recordings, fail the deliberately
degraded fixture, and fail closed in every case where it cannot tell.
"""

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.evals.__main__ import main
from app.platform import config

DEGRADED = EVALS / "fixtures" / "harness_smoke_degraded.json"


def run_row(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT suite::text AS suite, git_sha, config, status::text AS status, metrics, is_gate_passed, "
                    "finished_at FROM eval_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )


def item_rows(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT item_key, expected, actual, is_passed, score FROM eval_items "
                    "WHERE eval_run_id = :id ORDER BY id"
                ),
                {"id": run_id},
            )
            .mappings()
            .all()
        )


def test_a_run_with_the_shipped_recordings_records_the_run_and_every_item(engine):
    done = runner.run_suite("harness_smoke")
    run = run_row(engine, done.run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == ("harness_smoke", "passed", True)
    assert run["metrics"] == {"n": 5, "passed": 5, "score": 1.0, "harness_check_only": True}
    assert run["finished_at"] is not None and run["git_sha"]
    assert run["config"]["provider"] == "fake" and run["config"]["model"] == "recorded"
    assert run["config"]["prompt_version"] == runner.SUITES["harness_smoke"].prompt_version
    items = item_rows(engine, done.run_id)
    assert [i["item_key"] for i in items] == [f"smoke-0{n}" for n in range(1, 6)]
    assert all(i["is_passed"] and i["score"] == 1.0 and i["actual"] == i["expected"] for i in items)


def test_the_gate_passes_the_shipped_run_and_says_it_is_only_a_harness_check(engine, capsys):
    done = runner.run_suite("harness_smoke")
    assert gate.main("harness_smoke", done.run_id) == 0
    out = capsys.readouterr().out
    assert "scored 1.000 (5 of 5 items), floor 1.000: PASS" in out and gate.HARNESS_NOTE in out


def test_the_degraded_fixture_scores_below_the_floor_and_the_gate_names_the_items_that_failed(engine, capsys):
    done = runner.run_suite("harness_smoke", DEGRADED)
    run = run_row(engine, done.run_id)
    assert (run["status"], run["is_gate_passed"], run["metrics"]["score"]) == ("failed", False, 0.4)
    assert gate.main("harness_smoke", done.run_id) == 1
    out = capsys.readouterr().out
    assert "scored 0.400 (2 of 5 items), floor 1.000: FAIL" in out
    assert "items that failed: smoke-03, smoke-04, smoke-05" in out


def insert_run(engine, suite, status="passed", metrics='{"n": 1, "passed": 1, "score": 1.0}'):
    """A run row written directly, for the cases the runner itself never produces."""
    finished = "now()" if status != "running" else "NULL"
    with engine.begin() as conn:
        return conn.execute(
            text(
                f"INSERT INTO eval_runs (suite, git_sha, status, metrics, finished_at) VALUES "
                f"(CAST(:suite AS eval_suite), 'test', CAST(:status AS eval_status), "
                f"CAST(:metrics AS jsonb), {finished}) RETURNING id"
            ),
            {"suite": suite, "status": status, "metrics": None if status == "running" else metrics},
        ).scalar_one()


def test_a_suite_with_no_floor_in_thresholds_fails_the_gate(engine, capsys, monkeypatch, tmp_path):
    (tmp_path / "thresholds.toml").write_text("", encoding="utf-8")  # a floor file that lists no suite at all
    monkeypatch.setattr(gate, "EVALS", tmp_path)  # so this holds however many suites are registered in the real one
    run_id = insert_run(engine, "resume_extraction")
    assert gate.main("resume_extraction", run_id) == 1
    assert "no floor" in capsys.readouterr().out


def test_the_gate_fails_closed_when_it_cannot_tell(engine, capsys):
    assert gate.main("harness_smoke", uuid.uuid4()) == 1  # no such run
    assert gate.main("harness_smoke", insert_run(engine, "harness_smoke", "running")) == 1  # not finished
    assert gate.main("harness_smoke", insert_run(engine, "resume_extraction")) == 1  # another suite's run
    errored = insert_run(engine, "harness_smoke", "error", '{"n": 0, "passed": 0, "score": 0.0}')
    assert gate.main("harness_smoke", errored) == 1  # ended in an error
    out = capsys.readouterr().out
    for reason in ("does not exist", "did not finish cleanly", "belongs to suite resume_extraction"):
        assert reason in out


def test_a_recording_that_runs_out_ends_the_run_in_error_and_the_gate_refuses_it(engine, tmp_path):
    short = tmp_path / "short.json"
    short.write_text(json.dumps([{"kind": "request"}, {"kind": "thanks"}]), encoding="utf-8")
    done = runner.run_suite("harness_smoke", short)
    run = run_row(engine, done.run_id)
    assert (done.status, run["status"], run["is_gate_passed"]) == ("error", "error", None)
    assert run["metrics"]["error_code"] == "RuntimeError" and run["metrics"]["n"] == 2
    assert gate.main("harness_smoke", done.run_id) == 1


def test_an_answer_that_does_not_fit_the_schema_twice_fails_only_that_item(engine, tmp_path):
    answers = [{"kind": "request"}, "not json", "still not json", {"kind": "question"}, {"kind": "statement"}]
    answers.append({"kind": "greeting"})
    recording = tmp_path / "invalid.json"
    recording.write_text(json.dumps(answers), encoding="utf-8")
    done = runner.run_suite("harness_smoke", recording)
    items = {i["item_key"]: i for i in item_rows(engine, done.run_id)}
    assert [k for k, i in items.items() if not i["is_passed"]] == ["smoke-02"]
    assert items["smoke-02"]["actual"] is None and run_row(engine, done.run_id)["metrics"]["score"] == 0.8
    assert gate.main("harness_smoke", done.run_id) == 1  # 0.8 is under the floor of 1.0


def test_a_replay_refuses_a_real_provider_and_writes_nothing(engine, monkeypatch):
    real = replace(config.get(), llm_provider="anthropic")
    monkeypatch.setattr(config, "get", lambda: real)
    with pytest.raises(runner.ReplayNeedsFake):
        runner.run_suite("harness_smoke")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eval_runs")).scalar_one() == 0


def test_an_unknown_suite_is_refused_before_anything_is_written(engine, capsys):
    assert main(["run", "no_such_suite"]) == 2
    assert "unknown suite" in capsys.readouterr().err


def test_the_command_line_runs_then_gates_and_the_last_line_is_the_run_id(engine, capsys):
    assert main(["run", "harness_smoke"]) == 0
    run_id = capsys.readouterr().out.strip().splitlines()[-1]
    assert uuid.UUID(run_id)
    assert main(["gate", "harness_smoke", "--run", run_id]) == 0
    assert main(["run", "harness_smoke", "--recorded", str(DEGRADED)]) == 0  # recording a bad run is not an error
    bad_id = capsys.readouterr().out.strip().splitlines()[-1]
    assert main(["gate", "harness_smoke", "--run", bad_id]) == 1


def test_a_repeated_gold_key_is_refused(monkeypatch, tmp_path):
    (tmp_path / "gold").mkdir()
    (tmp_path / "gold" / "harness_smoke.jsonl").write_text(
        '{"key": "a", "text": "x", "expected": {}}\n{"key": "a", "text": "y", "expected": {}}\n', encoding="utf-8"
    )
    monkeypatch.setattr(runner, "EVALS", tmp_path)
    with pytest.raises(ValueError, match="repeated key"):
        runner.load_gold("harness_smoke")


def test_the_smoke_suite_is_labelled_a_harness_check_everywhere_it_is_described():
    readme = (EVALS / "README.md").read_text(encoding="utf-8")
    assert "harness_smoke" in readme and "never a quality result" in readme and "invented" in readme
    assert "harness_smoke" in runner.HARNESS_ONLY
