"""MAT-01, T-04-23: the constraint_property suite records its run, and the gate it feeds is absolute (plan 04-04).

The runner writes the run on connections of its own, so these tests use the eval tests' private database (the `engine`
fixture of tests/evals/conftest.py). The suite inserts its invented jobs and people through DATABASE_URL_MIGRATE and
rolls them back, so the only rows a run leaves are its eval_runs and eval_items. Nothing here uses a model.
"""

import importlib.util
import re
import sys
from uuid import UUID

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.evals.__main__ import main
from app.match import checker

SUITE = "constraint_property"
SUITE_DIR = EVALS / SUITE


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("constraint_property_score_for_suite_tests", SUITE_DIR / "score.py")


def run_row(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT suite::text AS suite, status::text AS status, config, metrics, is_gate_passed, finished_at "
                    "FROM eval_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_a_clean_run_meets_both_bars_records_one_item_per_constraint_set_and_passes_the_gate(engine, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.passed, done.n) == ("passed", 60, 60)
    run = run_row(engine, done.run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == (SUITE, "passed", True) and run["finished_at"]
    metrics = run["metrics"]
    assert metrics["score"] == 1.0 and metrics["constraint_violations"] == 0 and metrics["cases"] >= 10_000
    assert (metrics["jobs"], metrics["constraint_sets"], metrics["cases"]) == (240, 60, 14_400)
    assert metrics["bars"] == {"constraint_violations": True, "cases": True} and metrics["bars_met"] is True
    assert set(metrics["coverage"]) == set(score.cases.KINDS) and metrics["seed"] == score.SEED
    assert run["config"]["seed"] == score.SEED and run["config"]["model_calls"] == 0
    with engine.connect() as conn:
        items = conn.execute(
            text("SELECT item_key, is_passed, expected, actual FROM eval_items WHERE eval_run_id = :id ORDER BY id"),
            {"id": done.run_id},
        ).all()
        calls = conn.execute(text("SELECT count(*) FROM llm_calls")).scalar_one()
    assert [row.item_key for row in items] == [f"cs-{n:03d}" for n in range(60)] and all(row.is_passed for row in items)
    assert items[0].expected == {"disagreements": 0} == {"disagreements": items[0].actual["disagreements"]}
    assert calls == 0  # no model call, ever
    assert gate.main(SUITE, done.run_id) == 0
    assert "scored 1.000 (60 of 60 items), floor 1.000: PASS" in capsys.readouterr().out


def test_the_invented_rows_are_rolled_back_so_a_run_leaves_only_its_own_record(engine):
    runner.run_suite(SUITE)
    assert [count(engine, t) for t in ("users", "constraints", "jobs", "job_sources", "profile_events")] == [0] * 5
    assert count(engine, "eval_runs") == 1


def test_a_checker_that_ignores_excluded_companies_records_disagreements_scores_zero_and_fails_the_gate(
    engine, monkeypatch, capsys
):
    real = checker.violations
    monkeypatch.setattr(checker, "violations", lambda job, c: [k for k in real(job, c) if k != "excluded_company"])
    done = runner.run_suite(SUITE)
    run = run_row(engine, done.run_id)
    assert (done.status, run["is_gate_passed"]) == ("failed", False)
    metrics = run["metrics"]
    assert metrics["constraint_violations"] > 0 and metrics["score"] == 0.0 and metrics["cases"] >= 10_000
    assert metrics["bars"] == {"constraint_violations": False, "cases": True}
    capsys.readouterr()
    assert gate.main(SUITE, done.run_id) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "items that failed:" in out and "cs-007" in out  # set 7 holds only an excluded company
    with engine.connect() as conn:
        failed = conn.execute(
            text("SELECT actual FROM eval_items WHERE eval_run_id = :id AND item_key = 'cs-007'"), {"id": done.run_id}
        ).scalar_one()
    assert failed["disagreements"] > 0 and failed["first"][0]["field"] == "violations"
    assert "company_name" in failed["first"][0]["job"]  # the first disagreements are kept with their inputs


def test_fewer_than_ten_thousand_pairs_fail_the_gate_even_when_nothing_disagrees(engine, monkeypatch, capsys):
    monkeypatch.setattr(sys.modules["constraint_property_cases"], "N_JOBS", 100)  # 100 x 60 = 6,000 pairs
    done = runner.run_suite(SUITE)
    metrics = run_row(engine, done.run_id)["metrics"]
    assert (metrics["constraint_violations"], metrics["cases"]) == (0, 6_000)
    assert metrics["bars"] == {"constraint_violations": True, "cases": False} and metrics["score"] == 0.0
    assert done.status == "failed" and gate.main(SUITE, done.run_id) == 1


def test_a_database_that_is_not_a_scratch_one_ends_the_run_in_error_before_any_row_is_written(
    engine, monkeypatch, capsys
):
    monkeypatch.setattr(
        score.scratch, "SCRATCH_NAME", re.compile("never-matches")
    )  # the module the runner's copy uses too
    done = runner.run_suite(SUITE)
    assert done.status == "error" and run_row(engine, done.run_id)["metrics"]["error_code"] == "RuntimeError"
    assert [count(engine, t) for t in ("users", "jobs")] == [0, 0]
    assert gate.main(SUITE, done.run_id) == 1 and "did not finish cleanly" in capsys.readouterr().out


def test_the_command_line_runs_the_suite_and_the_last_line_is_the_run_id(engine, capsys):
    assert main(["run", SUITE]) == 0
    out = capsys.readouterr()
    assert UUID(out.out.strip().splitlines()[-1]) and f"{SUITE}: 60 of 60 items passed, status passed" in out.err
    assert main(["gate", SUITE, "--run", out.out.strip().splitlines()[-1]]) == 0


# ---- the gate's configuration and the documentation of the bars ----------------------------------------------------


def test_the_floor_the_registration_and_the_ci_listing_are_in_place():
    assert gate.floor(SUITE) == 1.0  # score is 1.0 only when both bars hold
    assert runner.SUITES[SUITE] == runner.OwnScorer("constraint_property/score.py")
    ci = (EVALS.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"suite: \[[^\]]*\bconstraint_property\b[^\]]*\]", ci)
    assert 'python -m app.evals run "${{ matrix.suite }}"' in ci  # the generic step runs it, then the generic gate


def test_the_bars_are_dated_written_before_any_run_and_absolute():
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    bars = " ".join(readme.split("## Pre-registered bars")[1].split("## Files")[0].split())
    assert "Written on 2026-11-23 (AI_SYSTEM 12.1" in bars
    assert "No run of this suite existed when these bars were written" in bars
    assert "`constraint_violations = 0`" in bars and "`cases >= 10000`" in bars
    assert "absolute" in bars and "`min_score = 1.0`" in bars
    thresholds = (EVALS / "thresholds.toml").read_text(encoding="utf-8")
    assert re.search(r"\[constraint_property\]\s*\nmin_score = 1\.0", thresholds)


@pytest.mark.parametrize("script", ["cases.py", "score.py", "README.md"])
def test_the_suite_has_its_files(script):
    assert (SUITE_DIR / script).is_file()
