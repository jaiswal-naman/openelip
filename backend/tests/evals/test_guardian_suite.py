"""GRD-01, GRD-02, EVL-01 (plan 07-03): the guardian suite replays 60 invented outside-action cases through the real
policy engine and the six real checks, and fails the gate when any one part is switched off.

The gold set is agent-drafted and its recorded answers are built from the labels, so every number here proves the code
path and says nothing about a model (invariant 8). The runs use the private database and the fake provider that
tests/evals/conftest.py sets up; nothing calls the network.
"""

import filecmp
import importlib.util
import json
import sys
from collections import Counter

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.guardian import checks, explain, policy

SUITE = "guardian"
SUITE_DIR = EVALS / SUITE
MARKER = "agent-drafted, maintainer review pending"
CODES = [
    *("dial_never", "campaign_paused", "campaign_closed", "account_inactive", "type_not_available"),
    *("citation_unresolved", "unsupported_claim", "citation_check_missing", "consent_missing"),
    *("constraint_conflict", "employer_blocked", "restricted_data", "budget_exhausted"),
    *("injection_suspected", "limit_reached"),
]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("guardian_score", SUITE_DIR / "score.py")
build = load("guardian_build", SUITE_DIR / "build.py")
GOLD = score.load_gold()
DEGRADED = sorted((SUITE_DIR / "degraded").glob("*.json"))


def metrics_of(engine, run_id):
    with engine.connect() as c:
        return c.execute(text("SELECT metrics FROM eval_runs WHERE id = :i"), {"i": run_id}).scalar_one()


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_sixty_agent_drafted_cases_half_of_them_blocked():
    assert len(GOLD) == 60 and len({case["key"] for case in GOLD}) == 60
    assert {case["labels"] for case in GOLD} == {MARKER}
    assert Counter(case["expect"]["blocked"] for case in GOLD) == {True: 30, False: 30}
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    assert "wiring" in readme and "agent-drafted" in readme and "ever published" in readme


def test_every_reason_code_and_every_check_is_covered_by_a_blocked_case():
    expected = {code for case in GOLD for code in case["expect"]["reasons"]}
    assert expected == set(CODES)
    assert all(code in explain.TEMPLATES for code in CODES)  # each has its sentence for the candidate
    assert set(CODES) - expected == set()
    by_check = {
        c: {case["key"] for case in GOLD if case["slice"] == c}
        for c in ("citations", "consent", "scope", "budget", "injection", "rate_limit")
    }
    assert all(by_check.values()), by_check
    assert len(checks.CHECK_KINDS) == 6


def test_a_case_not_labelled_blocked_has_no_reasons_and_a_blocked_one_has_at_least_one():
    for case in GOLD:
        assert bool(case["expect"]["reasons"]) == case["expect"]["blocked"], case["key"]
    assert not any("@" in json.dumps(case) and ".example" not in json.dumps(case) for case in GOLD)


def test_the_committed_files_are_what_the_build_script_writes(tmp_path):
    build.build(tmp_path)
    assert (tmp_path / "gold.jsonl").read_bytes() == (SUITE_DIR / "gold.jsonl").read_bytes()
    same = filecmp.dircmp(tmp_path / "recorded", SUITE_DIR / "recorded")
    assert not (same.left_only or same.right_only or same.diff_files)


# ---- the replay ----------------------------------------


def test_a_clean_replay_meets_every_bar_and_passes_the_gate(engine, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.n, done.passed) == ("passed", 60, 60)
    metrics = metrics_of(engine, done.run_id)
    assert metrics["score"] == 1.0 and metrics["bars_met"] and metrics["harness_check_only"]
    assert (metrics["guardian_block_recall"], metrics["guardian_false_blocks"], metrics["guardian_reason_match"]) == (
        1.0,
        0,
        1.0,
    )
    assert sorted(metrics["reason_codes_seen"]) == sorted(CODES) and (metrics["n_blocked"], metrics["n_allowed"]) == (
        30,
        30,
    )
    assert gate.main(SUITE, done.run_id) == 0
    assert "PASS" in capsys.readouterr().out


@pytest.mark.parametrize("fixture", DEGRADED, ids=lambda p: p.stem)
def test_a_degraded_fixture_with_one_part_switched_off_fails_the_gate(engine, capsys, fixture):
    done = runner.run_suite(SUITE, fixture)
    metrics = metrics_of(engine, done.run_id)
    assert (done.status, metrics["score"]) == ("failed", 0.0)
    assert metrics["guardian_block_recall"] < 1.0
    assert gate.main(SUITE, done.run_id) == 1
    assert "FAIL" in capsys.readouterr().out


def test_there_is_a_degraded_fixture_for_the_policy_engine_and_each_of_the_six_checks():
    assert [p.stem for p in DEGRADED] == sorted(f"no_{part}" for part in ("policy", *checks.CHECK_KINDS))


@pytest.mark.parametrize("part", ["scope", "consent", "budget", "rate_limit", "injection", "citations"])
def test_with_one_check_switched_off_in_the_code_the_gate_fails(engine, monkeypatch, part):
    passing = checks.CheckResult(part, True, None, {})
    monkeypatch.setattr(checks, f"check_{part}", lambda *a, **k: passing)
    done = runner.run_suite(SUITE)
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_with_the_policy_engine_switched_off_in_the_code_the_gate_fails(engine, monkeypatch):
    monkeypatch.setattr(policy, "decide", lambda action_type, *, dial, gate: policy.PolicyResult("ask"))
    done = runner.run_suite(SUITE)
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_with_a_check_too_strict_an_allowed_case_is_a_false_block_and_the_gate_fails(engine, monkeypatch):
    monkeypatch.setattr(
        checks, "check_rate_limit", lambda inp: checks.CheckResult("rate_limit", False, "limit_reached", {})
    )
    done = runner.run_suite(SUITE)
    assert metrics_of(engine, done.run_id)["guardian_false_blocks"] > 0
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_an_item_whose_recording_is_missing_ends_the_run_in_error_and_the_gate_refuses_it(engine, tmp_path):
    done = runner.run_suite(SUITE, tmp_path)
    assert done.status == "error" and gate.main(SUITE, done.run_id) == 1


def test_the_floor_the_registry_and_the_matrix_are_in_place():
    assert gate.floor(SUITE) == 1.0 and isinstance(runner.SUITES[SUITE], runner.OwnScorer)
    workflow = (EVALS.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    line = next(row for row in workflow.splitlines() if row.strip().startswith("suite: ["))
    assert SUITE in [name.strip() for name in line.split("[", 1)[1].split("]", 1)[0].split(",")]
