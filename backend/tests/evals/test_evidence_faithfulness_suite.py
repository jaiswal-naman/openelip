"""MAT-03, MAT-05, EVL-02: the evidence_faithfulness suite grades the match evidence path honestly, replays a recording
through the real request builder, gateway and citation validator, and fails the gate on a damaged one.

The gold set is 30 invented-resume and real-posting pairs with agent-drafted labels; its recording is built from those
labels, so every number here proves the code path and says nothing about a model (invariant 8). Tests that replay run
the real gateway and the real fake provider against the eval tests' private database (the `engine` fixture); the tests
of the gold set itself read files only.
"""

import importlib.util
import json
import re
import sys
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.match.evidence import build_request
from app.match.validator import Rejected, VerdictSet, _terms, validate
from app.platform.llm import fake

SUITE = "evidence_faithfulness"
SUITE_DIR = EVALS / SUITE
KINDS = {"skill", "experience", "education", "certification", "location", "other"}  # the kinds Phase 2's labels hold
MARKER = "agent-drafted, maintainer review pending"


def load(name, path):
    """The suite's scripts by a name of their own: the requirement suite's score.py is also called score."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("evidence_faithfulness_score", SUITE_DIR / "score.py")
build_gold = load("evidence_faithfulness_build_gold", SUITE_DIR / "build_gold.py")


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


GOLD = jsonl(SUITE_DIR / "gold.jsonl")
RECORDED = json.loads((SUITE_DIR / "recorded.json").read_text(encoding="utf-8"))
DEGRADED = json.loads((SUITE_DIR / "fixtures" / "degraded.json").read_text(encoding="utf-8"))
POSTINGS = {row["posting_id"]: row for row in jsonl(EVALS / "requirement_extraction" / "gold.jsonl")}
RESUMES = {row["key"]: row for row in jsonl(EVALS / "resume_extraction" / "facts" / "gold.jsonl")}


def request_of(item):
    return build_request(
        item["facts"],
        item["requirements"],
        description=POSTINGS[item["posting_ref"]]["description"],
        today=date.fromisoformat(item["as_of"]),
    )


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_thirty_pairs_every_one_marked_as_agent_drafted():
    assert len(GOLD) == 30 and len({item["key"] for item in GOLD}) == 30
    assert {item["labels"] for item in GOLD} == {MARKER}
    assert len({item["resume_ref"] for item in GOLD}) == 10  # ten invented resumes, three postings each
    assert all(sum(item["resume_ref"] == r for item in GOLD) == 3 for r in {i["resume_ref"] for i in GOLD})
    assert len({item["posting_ref"] for item in GOLD}) == 30


def test_every_pair_has_one_to_twenty_requirements_and_a_verdict_for_each_under_the_aliases_build_request_assigns():
    for item in GOLD:
        request = request_of(item)
        assert 1 <= len(item["requirements"]) <= 20, item["key"]
        assert list(request.requirements) == [f"r{n}" for n in range(1, len(item["requirements"]) + 1)]
        assert [ref.id for ref in request.requirements.values()] == [r["id"] for r in item["requirements"]]
        assert [ref.id for ref in request.facts.values()] == [f["id"] for f in item["facts"]]  # f1 is the first fact
        verdicts = item["expected"]["verdicts"]
        assert [v["requirement"] for v in verdicts] == list(request.requirements), item["key"]
        for v in verdicts:
            assert v["verdict"] in ("met", "partly_met", "not_met", "no_evidence") and v["why"]
            assert set(v["facts"]) <= set(request.facts), item["key"]
            assert (v["verdict"] == "no_evidence") == (v["facts"] == []), item["key"]


def test_every_requirement_resolves_in_the_phase_2_posting_text_and_is_the_item_that_gold_set_labelled():
    for item in GOLD:
        posting = POSTINGS[item["posting_ref"]]
        assert len(item["requirements"]) == len(posting["requirements"]), item["key"]
        for ordinal, (row, labelled) in enumerate(zip(item["requirements"], posting["requirements"], strict=True), 1):
            span = row["span"]
            assert posting["description"][span["start"] : span["end"]] == row["text"], (item["key"], ordinal)
            assert row["ordinal"] == ordinal
            assert (row["text"], row["importance"], row["kind"], row["min_years"]) == (
                labelled["text"],
                labelled["importance"],
                labelled["kind"],
                labelled["min_years"],
            )


def test_every_fact_resolves_in_the_phase_3_resume_text_and_is_a_truth_fact_of_that_resume():
    for item in GOLD:
        resume = RESUMES[item["resume_ref"]]
        resume_text = "\n".join(resume["lines"])
        assert len(item["facts"]) == len(resume["expected"]["facts"]), item["key"]
        for fact in item["facts"]:
            span = fact["provenance"]["span"]
            assert resume_text[span["start"] : span["end"]] == fact["statement"] == fact["provenance"]["quote"]
            assert fact["status"] == "confirmed" and fact["source"] == "resume"
        assert len({fact["id"] for fact in item["facts"]}) == len(item["facts"])


def test_the_set_holds_every_verdict_every_requirement_kind_and_about_a_third_poor_fits():
    verdicts = {v["verdict"] for item in GOLD for v in item["expected"]["verdicts"]}
    assert verdicts == {"met", "partly_met", "not_met", "no_evidence"}
    assert {r["kind"] for item in GOLD for r in item["requirements"]} == KINDS
    assert {r["importance"] for item in GOLD for r in item["requirements"]} == {"required", "preferred"}
    poor = sum(item["fit"] == "poor" for item in GOLD)
    assert 8 <= poor <= 12 and {item["fit"] for item in GOLD} == {"poor", "partial", "close"}
    assert {item["slice"] for item in GOLD} == {"skill_led", "experience_led", "mixed"}


def test_a_not_met_verdict_is_only_ever_a_years_requirement_beyond_the_career():
    for item in GOLD:
        request = request_of(item)
        for v in item["expected"]["verdicts"]:
            if v["verdict"] == "not_met":
                requirement = request.requirements[v["requirement"]]
                assert requirement.min_years and requirement.min_years * 12 > request.career_months, item["key"]


def test_no_fact_or_posting_carries_an_email_address_or_a_phone_number():
    for item in GOLD:
        assert not re.search(r"[\w.+-]+@[\w-]+\.\w", POSTINGS[item["posting_ref"]]["description"]), item["key"]
        for fact in item["facts"]:
            assert "@" not in fact["statement"]
            assert build_request([fact], [], description="").facts["f1"].statement == " ".join(
                fact["statement"].split()
            )


def test_gold_jsonl_is_what_build_gold_builds_from_the_labels_and_the_two_gold_sets():
    assert build_gold.render(build_gold.build(*build_gold.load_inputs())) == (SUITE_DIR / "gold.jsonl").read_text(
        encoding="utf-8"
    )
    assert build_gold.main(["--check"]) == 0


def test_the_recording_is_the_expected_verdicts_of_every_pair_in_pair_order_and_the_gold_why_is_not_in_it():
    assert RECORDED == [
        {"verdicts": [{k: v[k] for k in ("requirement", "verdict", "facts")} for v in item["expected"]["verdicts"]]}
        for item in GOLD
    ]
    assert len(RECORDED) == 30 and all("why" not in v for entry in RECORDED for v in entry["verdicts"])


def test_every_recorded_answer_is_accepted_by_the_validator_and_one_set_is_marked_weak():
    weak = 0
    for item, entry in zip(GOLD, RECORDED, strict=True):
        request = request_of(item)
        result = validate(VerdictSet.model_validate(entry), request, career_months=request.career_months)
        assert not isinstance(result, Rejected), (item["key"], result)
        weak += result.weak_fit
    assert weak == 1  # ev-004: Grafana met on "observability toolchains", which shares no word with it


# ---- the degraded recording ----------------------------------------


def test_the_degraded_recording_is_the_recording_with_five_false_mets_and_two_sets_that_cite_an_alias_never_sent():
    entries, wrong_met, rejected_twice = iter(DEGRADED), [], []
    for item, clean in zip(GOLD, RECORDED, strict=True):
        request = request_of(item)
        first = next(entries)
        if first == clean:
            continue
        changed = [
            (c["requirement"], c["verdict"], d["verdict"], d["facts"])
            for c, d in zip(clean["verdicts"], first["verdicts"], strict=True)
            if c != d
        ]
        answer = VerdictSet.model_validate(first)
        if isinstance(validate(answer, request, career_months=request.career_months), Rejected):
            assert next(entries) == first  # the fresh call after the rejection is no better
            rejected_twice.append(item["key"])
        else:
            assert len(changed) == 1
            alias, was, now, cited = changed[0]
            requirement = request.requirements[alias]
            unrelated = request.facts[cited[0]]
            assert (was, now) == ("no_evidence", "met") and len(cited) == 1
            assert requirement.importance == "required" and not (_terms(requirement.text) & _terms(unrelated.statement))
            wrong_met.append(item["key"])
    assert next(entries, None) is None  # every degraded entry was accounted for
    assert (len(wrong_met), len(rejected_twice)) == (5, 2) and len(DEGRADED) == 32


# ---- the scoring ----------------------------------------


def test_each_interval_is_a_95_percent_wilson_interval():
    assert score.wilson(0, 0) == [0.0, 1.0]
    low, high = score.wilson(30, 30)
    assert 0.88 < low < 0.89 and high == 1.0
    low, high = score.wilson(0, 30)
    assert low == 0.0 and 0.11 < high < 0.12  # about 3 over n: the upper bound with no event in 30 tries


@pytest.mark.parametrize(
    ("rates", "held"),
    [
        ((0.90, 0.05, 0.02), (True, True, True)),  # at a bar is a pass
        ((0.8999, 0.05, 0.02), (False, True, True)),
        ((0.90, 0.0501, 0.02), (True, False, True)),
        ((0.90, 0.05, 0.0201), (True, True, False)),
        ((1.0, 0.0, 0.0), (True, True, True)),
        ((0.0, 0.0, 0.0), (False, True, True)),  # nothing accepted: nothing faithful
    ],
)
def test_each_pre_registered_bar_is_checked_at_its_own_value_and_alone(rates, held):
    assert tuple(score.bars_held(*rates).values()) == held
    assert (score.FAITHFUL_BAR, score.FALSE_MET_BAR, score.REJECT_BAR) == (0.90, 0.05, 0.02)


def recording(tmp_path, entries):
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def edited(change):
    """A copy of the recording after change(entries, items) edited it."""
    entries = json.loads(json.dumps(RECORDED))
    change(entries)
    return entries


def replay(path):
    return score.run(path)


def test_a_clean_replay_has_every_verdict_equal_to_gold_and_meets_every_bar(engine):
    scored = replay(SUITE_DIR / "recorded.json")
    metrics = scored.metrics
    assert (metrics["pairs"], metrics["accepted_pairs"], metrics["verdicts"], metrics["faithful_verdicts"]) == (
        30,
        30,
        287,
        287,
    )
    assert (metrics["required_items"], metrics["false_met_required"], metrics["rejected_pairs"]) == (234, 0, 0)
    assert (metrics["evidence_faithful_rate"], metrics["false_met_required_rate"], metrics["citation_reject_rate"]) == (
        1.0,
        0.0,
        0.0,
    )
    assert metrics["bars_met"] is True and metrics["score"] == 1.0 and metrics["harness_check_only"] is True
    assert all(r.is_passed for r in scored.results) and [r.key for r in scored.results] == [g["key"] for g in GOLD]
    assert fake.call("ping").text  # the fake provider is back on its own recordings after a run


def test_one_pair_rejected_twice_fails_the_rejection_bar_and_only_that_bar(engine, tmp_path):
    def unknown_fact_twice(entries):
        entries[0]["verdicts"][0] = {"requirement": "r1", "verdict": "met", "facts": ["f999"]}
        entries.insert(1, json.loads(json.dumps(entries[0])))

    metrics = replay(recording(tmp_path, edited(unknown_fact_twice))).metrics
    assert (metrics["rejected_pairs"], metrics["retried_pairs"], metrics["accepted_pairs"]) == (1, 1, 29)
    assert metrics["bars"] == {
        "evidence_faithful_rate": True,
        "false_met_required_rate": True,
        "citation_reject_rate": False,
    }
    assert (metrics["bars_met"], metrics["score"]) == (False, 0.0)


def test_a_set_rejected_once_and_right_the_second_time_is_accepted_and_counted_as_a_retry(engine, tmp_path):
    def wrong_first(entries):
        entries.insert(0, {"verdicts": [{"requirement": "r9", "verdict": "no_evidence", "facts": []}]})

    scored = replay(recording(tmp_path, edited(wrong_first)))
    assert (scored.metrics["retried_pairs"], scored.metrics["accepted_pairs"], scored.metrics["score"]) == (1, 30, 1.0)
    assert scored.results[0].is_passed and scored.results[0].actual["tries"] == 2


def test_an_answer_that_does_not_fit_the_schema_twice_fails_that_pair_and_the_run_goes_on(engine, tmp_path):
    scored = replay(recording(tmp_path, ["not json", "still not json", *RECORDED[1:]]))
    assert scored.metrics["schema_invalid_pairs"] == 1 and scored.metrics["accepted_pairs"] == 29
    assert scored.results[0].is_passed is False and scored.results[0].actual["failed"] == "schema_invalid"
    assert all(r.is_passed for r in scored.results[1:])


def test_too_many_false_mets_on_required_items_fail_the_false_met_bar_and_only_that_bar(engine, tmp_path):
    flipped = []

    def many_false_mets(entries):
        for item, entry in zip(GOLD, entries, strict=True):
            request = request_of(item)
            for verdict in entry["verdicts"]:
                requirement = request.requirements[verdict["requirement"]]
                if (
                    len(flipped) < 13
                    and verdict["verdict"] == "no_evidence"
                    and requirement.importance == "required"
                    and requirement.min_years is None
                ):
                    verdict["verdict"], verdict["facts"] = "met", ["f1"]
                    flipped.append(item["key"])
                    break

    metrics = replay(recording(tmp_path, edited(many_false_mets))).metrics
    assert (metrics["false_met_required"], metrics["rejected_pairs"]) == (13, 0)
    assert metrics["bars"] == {
        "evidence_faithful_rate": True,
        "false_met_required_rate": False,
        "citation_reject_rate": True,
    }
    assert metrics["score"] == 0.0


def test_a_false_met_on_a_preferred_item_costs_faithfulness_but_is_not_a_false_met_on_a_required_item(engine, tmp_path):
    def one_preferred(entries):
        for item, entry in zip(GOLD, entries, strict=True):
            request = request_of(item)
            for verdict in entry["verdicts"]:
                preferred = request.requirements[verdict["requirement"]].importance == "preferred"
                if preferred and verdict["verdict"] == "no_evidence":
                    verdict["verdict"], verdict["facts"] = "met", ["f1"]
                    return

    metrics = replay(recording(tmp_path, edited(one_preferred))).metrics
    assert (metrics["false_met_required"], metrics["faithful_verdicts"], metrics["required_items"]) == (0, 286, 234)


def test_too_many_wrong_verdicts_fail_the_faithfulness_bar_and_only_that_bar(engine, tmp_path):
    wrong = []

    def many_wrong(entries):
        for item, entry in zip(GOLD, entries, strict=True):
            request = request_of(item)
            for verdict in entry["verdicts"]:
                requirement = request.requirements[verdict["requirement"]]
                if len(wrong) < 32 and verdict["verdict"] == "no_evidence" and requirement.importance == "preferred":
                    verdict["verdict"], verdict["facts"] = (
                        "partly_met",
                        ["f1"],
                    )  # not a false met: not "met", not required
                    wrong.append(item["key"])

    metrics = replay(recording(tmp_path, edited(many_wrong))).metrics
    assert len(wrong) == 32 and metrics["faithful_verdicts"] == 287 - 32
    assert metrics["bars"] == {
        "evidence_faithful_rate": False,
        "false_met_required_rate": True,
        "citation_reject_rate": True,
    }
    assert metrics["score"] == 0.0


# ---- recording a run and the gate ----------------------------------------


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


def test_a_clean_replay_is_recorded_as_one_run_with_one_item_per_pair_and_passes_the_gate(engine, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.passed, done.n) == ("passed", 30, 30)
    run = run_row(engine, done.run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == (SUITE, "passed", True) and run["finished_at"]
    assert run["config"] == {
        "mode": "replay",
        "recorded": "recorded.json",
        "task": "match.evidence.score",
        "provider": "fake",
        "model": "recorded",
        "labels": MARKER,
        "prompt_version": run["config"]["prompt_version"],
        "gold": "gold.jsonl",
    }
    assert len(run["config"]["prompt_version"]) == 12
    assert run["metrics"]["score"] == 1.0 and run["metrics"]["n"] == 30
    with engine.connect() as conn:
        items = conn.execute(
            text("SELECT item_key, is_passed, expected, actual FROM eval_items WHERE eval_run_id = :id ORDER BY id"),
            {"id": done.run_id},
        ).all()
        calls = conn.execute(
            text("SELECT count(*), min(outcome::text), max(outcome::text), min(task) FROM llm_calls")
        ).one()
    assert [row.item_key for row in items] == [item["key"] for item in GOLD] and all(row.is_passed for row in items)
    assert set(items[0].expected) == {"verdicts"} and items[0].expected == {"verdicts": items[0].actual["verdicts"]}
    assert tuple(calls) == (30, "ok", "ok", "match.evidence.score")  # one real gateway request per pair, no text stored
    assert gate.main(SUITE, done.run_id) == 0
    assert "scored 1.000 (30 of 30 items), floor 1.000: PASS" in capsys.readouterr().out


def test_the_degraded_recording_lowers_the_rates_records_score_zero_and_fails_the_gate_naming_the_pairs(engine, capsys):
    clean = run_row(engine, runner.run_suite(SUITE).run_id)["metrics"]
    done = runner.run_suite(SUITE, SUITE_DIR / "fixtures" / "degraded.json")
    run = run_row(engine, done.run_id)
    metrics = run["metrics"]
    assert (done.status, done.passed, run["is_gate_passed"]) == ("failed", 23, False)
    assert metrics["evidence_faithful_rate"] < clean["evidence_faithful_rate"]
    assert metrics["false_met_required_rate"] > clean["false_met_required_rate"]
    assert metrics["citation_reject_rate"] > clean["citation_reject_rate"]
    assert (metrics["false_met_required"], metrics["rejected_pairs"], metrics["retried_pairs"]) == (5, 2, 2)
    assert metrics["score"] == 0.0 and metrics["bars"]["citation_reject_rate"] is False
    capsys.readouterr()
    assert gate.main(SUITE, done.run_id) == 1
    out = capsys.readouterr().out
    assert "scored 0.000 (23 of 30 items), floor 1.000: FAIL" in out and "items that failed: ev-003" in out
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM llm_calls")).scalar_one() == 30 + 32
        )  # both runs, retries included


def test_a_recording_that_runs_out_ends_the_run_in_error_and_the_gate_refuses_it(engine, tmp_path, capsys):
    done = runner.run_suite(SUITE, recording(tmp_path, RECORDED[:5]))
    assert done.status == "error" and run_row(engine, done.run_id)["metrics"]["error_code"] == "RuntimeError"
    assert gate.main(SUITE, done.run_id) == 1
    assert "did not finish cleanly" in capsys.readouterr().out


def test_the_command_line_replays_records_and_prints_the_run_id_last(engine, capsys):
    assert score.main([]) == 0
    out = capsys.readouterr()
    assert UUID(out.out.strip().splitlines()[-1]) and "30 of 30 pairs passed, status passed" in out.err
    assert score.main(["--recorded", str(SUITE_DIR / "fixtures" / "degraded.json")]) == 0  # recording is not an error
    assert "23 of 30 pairs passed, status failed" in capsys.readouterr().err


def test_the_command_line_refuses_a_database_that_is_not_a_scratch_one(engine, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL_WORKER", "postgresql://app_worker:placeholder@127.0.0.1:5433/app")
    with pytest.raises(SystemExit) as refused:
        score.main([])
    assert refused.value.code == 2 and "'app'" in capsys.readouterr().err


# ---- the gate's configuration and the documentation of the bars ----------------------------------------


def test_the_floor_the_ci_listing_and_the_registration_are_in_place():
    assert gate.floor(SUITE) == 1.0  # score is 1.0 only when every bar holds
    assert runner.SUITES[SUITE] == runner.OwnScorer("evidence_faithfulness/score.py")
    ci = (EVALS.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"suite: \[[^\]]*\bevidence_faithfulness\b[^\]]*\]", ci)
    assert 'python -m app.evals run "${{ matrix.suite }}"' in ci  # the generic step runs it, then the generic gate


def test_the_bars_are_dated_written_before_any_run_and_say_what_a_replay_does_not_show():
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    bars = " ".join(readme.split("## Pre-registered bars")[1].split("## Commands")[0].split())
    assert "Written on 2026-11-23 (AI_SYSTEM 12.1" in bars
    assert "No run of this suite existed when these bars were written" in bars
    for bar in (
        "`evidence_faithful_rate >= 0.90`",
        "`false_met_required_rate <= 0.05`",
        "`citation_reject_rate <= 0.02`",
    ):
        assert bar in bars
    assert "`min_score = 1.0`" in bars and "anything about a model" in bars
    assert "not a model quality number" in readme and MARKER in readme and "invariant 8" in readme
    guideline = (SUITE_DIR / "GUIDELINE.md").read_text(encoding="utf-8")
    assert MARKER in guideline and "Decisions made while labelling" in guideline
