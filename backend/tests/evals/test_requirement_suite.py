"""IDX-08: the requirement_extraction suite scores extraction honestly, replays its recordings through the real task
path, and fails when a citation does not resolve or the replay falls below the stored baseline.

Most tests score hand-made postings, which need no database. The replay tests run the real gateway and the real fake
provider against the eval tests' private database (the `engine` fixture). None of them scores the test split of
evals/requirement_extraction/gold.jsonl, except the two that replay the committed recordings and rules exactly as the
CI gate does, to prove the stored baseline is reproduced; no code is tuned against what they read.
"""

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.evals import EVALS
from app.ingest.api import rules_extract
from app.ingest.requirements.validate import RequirementRow
from app.platform import config
from app.platform.llm import fake

SUITE_DIR = EVALS / "requirement_extraction"
sys.path.insert(0, str(SUITE_DIR))  # score.py and record_fixture.py import each other by name, as they do when run
import record_fixture  # noqa: E402
import score  # noqa: E402

VOLATILE = {"bars", "bars_met", "score"}  # what depends on the baseline a run is held to, not on the run itself

# Two invented postings, the gold items a person would label, and what an extractor might predict.
A = "Needs Python.\nNeeds SQL.\nNice to have Rust.\nWe offer lunch."
B = "Needs Go.\nNeeds Docker."


def row(description, quote, importance="required", ordinal=1):
    start = description.index(quote)
    return RequirementRow(ordinal, importance, "skill", quote, start, start + len(quote), None)


def posting(posting_id, description, *labels):
    """A gold posting: labels are (quote, importance)."""
    items = [
        {"start": description.index(q), "end": description.index(q) + len(q), "text": q, "importance": imp}
        for q, imp in labels
    ]
    return {"posting_id": posting_id, "split": "dev", "description": description, "requirements": items}


def fixed(rows_by_posting):
    return lambda found: (rows_by_posting[found["posting_id"]], {})


POSTINGS = [
    posting("a", A, ("Needs Python.", "required"), ("Needs SQL.", "required"), ("Nice to have Rust.", "preferred")),
    posting("b", B, ("Needs Go.", "required"), ("Needs Docker.", "preferred")),
]
PREDICTED = {
    "a": [
        row(A, "Needs Python."),  # exactly the gold item
        row(A, "Needs SQL", ordinal=2),  # one character short: overlap 0.9
        row(A, "Nice to have Rust.", "required", 3),  # the right words, the wrong importance
        row(A, "We offer lunch.", ordinal=4),  # nothing a person labelled
    ],
    "b": [
        row(B, "Needs Go."),
        row(B, "Docker", "preferred", 2),  # 6 of 13 characters: overlap 0.46, below the 0.5 that makes a match
    ],
}


# ---- how items are paired ----------------------------------------


def test_overlap_is_intersection_over_union_of_two_half_open_spans():
    assert score.iou((0, 10), (0, 10)) == 1.0
    assert score.iou((0, 10), (5, 15)) == pytest.approx(5 / 15)
    assert score.iou((0, 10), (2, 6)) == pytest.approx(4 / 10)  # one inside the other
    assert score.iou((0, 10), (10, 20)) == 0.0  # touching is not overlapping
    assert score.iou((0, 10), (30, 40)) == 0.0


def test_a_pair_is_a_match_at_exactly_half_overlap_and_not_below():
    assert score.match([(0, 5, "required")], [(0, 10, "required")]) == [(0, 0)]  # 5 of 10
    assert score.match([(0, 4, "required")], [(0, 10, "required")]) == []  # 4 of 10


def test_pairing_is_one_to_one_and_the_greatest_overlap_goes_first():
    gold = [(0, 10, "required")]
    assert score.match([(0, 8, "required"), (0, 10, "required")], gold) == [(1, 0)]  # the exact one wins, listed second
    assert score.match([(0, 10, "required"), (0, 10, "required")], gold) == [(0, 0)]  # a tie goes to the earlier item
    two_gold = [(0, 10, "required"), (6, 16, "required")]
    assert score.match([(0, 12, "required")], two_gold) == [(0, 0)]  # 10/12 against the first, 6/16 against the second
    assert score.match([(0, 10, "required"), (6, 16, "required")], [(6, 16, "required")]) == [(1, 0)]
    next_to_each_other = [(0, 10, "required"), (10, 20, "required")]  # one predicted item over both: half of each
    assert score.match([(0, 20, "required")], next_to_each_other) == [(0, 0)]  # it counts once, for the earlier one


# ---- the numbers ----------------------------------------


def test_two_postings_give_the_expected_precision_recall_f1_and_importance_accuracy():
    result = score.score(POSTINGS, fixed(PREDICTED))
    metrics = result["metrics"]
    # 6 predicted, 5 in the gold set, 4 matched (3 in a, 1 in b); of the 4 matched, 3 have the right importance.
    assert (metrics["n"], metrics["predicted_items"], metrics["gold_items"], metrics["matched_items"]) == (2, 6, 5, 4)
    assert metrics["precision"] == pytest.approx(4 / 6, abs=1e-4)
    assert metrics["recall"] == pytest.approx(4 / 5, abs=1e-4)
    assert metrics["req_item_f1"] == pytest.approx(8 / 11, abs=1e-4)
    assert metrics["req_importance_accuracy"] == pytest.approx(3 / 4, abs=1e-4)
    assert metrics["req_spans_resolved"] == 1.0 and metrics["bars_met"] is True and metrics["score"] == 1.0
    assert [(i["key"], i["is_passed"], i["score"]) for i in result["items"]] == [("a", True, 0.8571), ("b", True, 0.5)]
    assert result["items"][0]["expected"]["items"][0] == [0, 13, "required"]


def test_each_interval_is_a_95_percent_wilson_interval_for_its_own_count():
    metrics = score.score(POSTINGS, fixed(PREDICTED))["metrics"]
    assert metrics["intervals"]["precision"] == [round(end, 4) for end in score.wilson(4, 6)]
    assert metrics["intervals"]["recall"] == [round(end, 4) for end in score.wilson(4, 5)]
    assert metrics["intervals"]["req_importance_accuracy"] == [round(end, 4) for end in score.wilson(3, 4)]


def test_the_wilson_interval_matches_known_values():
    assert score.wilson(0, 0) == (0.0, 1.0)
    low, high = score.wilson(6, 6)
    assert 0.60 < low < 0.62 and high == pytest.approx(1.0)
    low, high = score.wilson(0, 6)
    assert low == 0.0 and 0.38 < high < 0.40


def test_items_of_one_posting_never_match_items_of_another():
    same_span = [
        posting("a", "Needs Python.", ("Needs Python.", "required")),
        posting("b", "Needs Go.", ("Needs Go.", "required")),
    ]
    metrics = score.score(same_span, fixed({"a": [], "b": [row("Needs Go.", "Needs Go.")]}))["metrics"]
    assert (metrics["matched_items"], metrics["recall"]) == (1, 0.5)  # a's gold item is not found in b's rows


def test_nothing_predicted_is_zero_recall_and_no_citation_that_fails_to_resolve():
    metrics = score.score(POSTINGS, fixed({"a": [], "b": []}))["metrics"]
    assert (metrics["req_item_f1"], metrics["recall"], metrics["req_importance_accuracy"]) == (0.0, 0.0, 0.0)
    assert metrics["req_spans_resolved"] == 1.0 and metrics["bars_met"] is True  # no bar asks for a score yet


def test_no_posting_meets_no_bar():
    assert score.score([], fixed({}))["metrics"]["bars_met"] is False


# ---- a citation that does not resolve fails the run ----------------------------------------


def test_a_row_whose_text_is_not_the_postings_text_at_its_span_fails_the_run_whatever_else_is_true():
    gold = [posting("a", "Needs Python.", ("Needs Python.", "required"))]
    fabricated = RequirementRow(1, "required", "skill", "Needs Rust.", 0, 13, None)  # the right span, other words
    result = score.score(gold, fixed({"a": [fabricated]}))
    metrics = result["metrics"]
    assert (metrics["req_item_f1"], metrics["req_importance_accuracy"]) == (1.0, 1.0)  # as good as it gets otherwise
    assert metrics["req_spans_resolved"] == 0.0 and metrics["bars"] == {"req_spans_resolved": False}
    assert (metrics["bars_met"], metrics["score"]) == (False, 0.0)
    assert result["items"][0]["is_passed"] is False  # the gate names this posting


@pytest.mark.parametrize(
    "words, start, end",
    [
        ("Needs Python.", -3, 10),
        ("on.", -3, 13),  # Python reads [-3:13] as the last three characters, which are these words; a span cannot
        ("Needs Python.", 0, 40),
        ("Needs Python.", 5, 5),
        ("", 5, 5),  # an empty span "quotes" nothing, and the table refuses it
        ("Needs Python.", 9, 4),
    ],
    ids=["negative", "negative_that_slices_true", "past_the_end", "empty_span", "empty_words", "backwards"],
)
def test_a_span_outside_the_posting_or_empty_does_not_resolve(words, start, end):
    gold = [posting("a", "Needs Python.", ("Needs Python.", "required"))]
    metrics = score.score(gold, fixed({"a": [RequirementRow(1, "required", "skill", words, start, end, None)]}))[
        "metrics"
    ]
    assert metrics["req_spans_resolved"] == 0.0 and metrics["bars_met"] is False


def test_one_unresolved_row_among_thousands_still_fails_and_is_not_rounded_to_one():
    gold = [posting("a", "Needs Python.", ("Needs Python.", "required"))]
    good = row("Needs Python.", "Needs Python.")
    bad = RequirementRow(1, "required", "skill", "Needs Rust.", 0, 13, None)
    metrics = score.score(gold, fixed({"a": [good] * 20_000 + [bad]}))["metrics"]
    assert metrics["req_spans_resolved"] < 1.0 and metrics["bars"]["req_spans_resolved"] is False


# ---- the stored baseline ----------------------------------------


def test_a_replay_may_equal_or_beat_the_stored_baseline_and_may_not_fall_below_it():
    base = score.score(POSTINGS, fixed(PREDICTED))["metrics"]
    assert score.score(POSTINGS, fixed(PREDICTED), baseline=base)["metrics"]["bars_met"] is True  # equal
    lower = {**base, "req_item_f1": 0.5, "req_importance_accuracy": 0.5}
    assert score.score(POSTINGS, fixed(PREDICTED), baseline=lower)["metrics"]["bars_met"] is True  # better

    higher_f1 = {**base, "req_item_f1": base["req_item_f1"] + 0.0001}
    metrics = score.score(POSTINGS, fixed(PREDICTED), baseline=higher_f1)["metrics"]
    assert metrics["bars"] == {
        "req_spans_resolved": True,
        "req_item_f1_not_below_baseline": False,
        "req_importance_accuracy_not_below_baseline": True,
    }
    assert (metrics["bars_met"], metrics["score"]) == (False, 0.0)

    higher_importance = {**base, "req_importance_accuracy": base["req_importance_accuracy"] + 0.0001}
    assert score.score(POSTINGS, fixed(PREDICTED), baseline=higher_importance)["metrics"]["bars_met"] is False


def test_a_replay_is_held_only_to_a_stored_replay_of_its_own_split(tmp_path):
    path = tmp_path / "baseline.json"
    assert score.stored_baseline("model", "test", path) is None  # no file: nothing to fall below
    path.write_text(
        json.dumps({"runs": {"rules": {"config": {"split": "test"}, "metrics": {"req_item_f1": 0.1}}}}),
        encoding="utf-8",
    )
    assert score.stored_baseline("model", "test", path) is None  # a rules run is not what a replay is held to
    replay = {"config": {"split": "test"}, "metrics": {"req_item_f1": 0.7, "req_importance_accuracy": 0.9}}
    path.write_text(json.dumps({"runs": {"replay": replay}}), encoding="utf-8")
    assert score.stored_baseline("model", "test", path) == replay["metrics"]
    assert score.stored_baseline("model", "dev", path) is None  # dev numbers are not compared with test numbers
    assert score.stored_baseline("rules", "test", path) is None  # the rules method carries no bar


# ---- which postings are scored ----------------------------------------


def test_the_test_split_leaves_out_the_near_copy_of_a_dev_posting_and_the_dev_split_keeps_its_own():
    test, dev = score.load_gold("test"), score.load_gold("dev")
    assert (len(dev), len(test)) == (10, 39)
    assert "req-049" in {p["posting_id"] for p in score.read_jsonl(score.GOLD)}  # it is still in the gold set...
    assert "req-049" not in {p["posting_id"] for p in test}  # ...and out of the test score
    assert "req-001" in {p["posting_id"] for p in dev}  # its twin stays in dev
    assert sum(len(p["requirements"]) for p in test) == 390  # 395 test items less req-049's 5
    assert not {p["posting_id"] for p in test} & {p["posting_id"] for p in dev}


# ---- the recordings ----------------------------------------


def test_the_committed_recordings_are_what_the_recorder_builds():
    assert score.read_jsonl(score.RECORDINGS) == record_fixture.build()


def test_every_recording_is_tagged_a_fixture_and_every_gold_posting_has_one():
    lines = score.read_jsonl(score.RECORDINGS)
    assert {line["source"] for line in lines} == {"rules_fixture"}
    assert [line["posting_id"] for line in lines] == [p["posting_id"] for p in score.read_jsonl(score.GOLD)]


def test_replaying_the_recordings_through_the_task_gives_back_exactly_the_rules_rows_for_all_50_postings(engine):
    """The recorder, the fake provider, the task, the validator and the span computation agree with each other."""
    with tempfile.TemporaryDirectory() as folder:
        replay = score.replay_extractor(score.load_recordings(), Path(folder))
        try:
            for found in score.read_jsonl(score.GOLD):
                rows, problems = replay(found)
                assert problems == {}
                assert list(rows) == rules_extract(found["description"]), found["posting_id"]
        finally:
            fake.replay_from(None)
    with engine.connect() as conn:  # and every posting went through the real gateway, one request each
        calls = conn.execute(text("SELECT count(*), min(outcome::text), max(outcome::text) FROM llm_calls")).one()
    assert tuple(calls) == (50, "ok", "ok")


def write_recordings(tmp_path, change):
    lines = score.read_jsonl(score.RECORDINGS)
    change(lines)
    path = tmp_path / "recordings.jsonl"
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")
    return path


def the_fake_provider_is_back_to_its_own_recordings():
    """A replay points the fake provider at one posting's answers; when it ends, ping is answered from ping.json."""
    return json.loads(fake.call("ping").text) == {"reply": "pong"}


def test_a_recording_made_for_another_request_is_refused_and_says_what_to_do(engine, tmp_path):
    path = write_recordings(tmp_path, lambda lines: lines[0].update(request_sha256="0" * 64))
    with pytest.raises(score.RecordingError, match="req-001: recorded for another request"):
        score.run("model", "dev", recordings=path)
    assert the_fake_provider_is_back_to_its_own_recordings()  # also after a run that stopped half way


def test_a_posting_with_no_recording_is_refused(engine, tmp_path):
    path = write_recordings(tmp_path, lambda lines: lines.pop(0))
    with pytest.raises(score.RecordingError, match="req-001: no recorded answer"):
        score.run("model", "dev", recordings=path)


def test_only_fixture_recordings_are_judged_here(engine, tmp_path):
    path = write_recordings(tmp_path, lambda lines: lines[0].update(source="live"))
    with pytest.raises(score.RecordingError, match="only fixture replays are scored"):
        score.run("model", "dev", recordings=path)


BAD_ITEM = {"segments": [999], "quote": None, "importance": "required", "kind": "skill", "min_years": None}


def test_an_answer_with_no_usable_item_asks_again_and_an_answer_invalid_twice_fails_only_that_posting(engine, tmp_path):
    once = write_recordings(tmp_path, lambda lines: lines[0].update(answers=[{"requirements": [BAD_ITEM]}]))
    with pytest.raises(score.RecordingError, match="req-001: the task asked for more answers than were recorded"):
        score.run("model", "dev", recordings=once)

    twice = write_recordings(tmp_path, lambda lines: lines[0].update(answers=[{"requirements": [BAD_ITEM]}] * 2))
    result = score.run("model", "dev", recordings=twice)  # the run goes on without that posting's rows
    assert result["metrics"]["rejected"] == {"extraction_failed": 1}
    assert [i["key"] for i in result["items"] if not i["is_passed"]] == ["req-001"]
    assert result["metrics"]["n"] == 10 and result["metrics"]["passed"] == 9


def a_recorded_item_that_matches_a_label():
    """(posting id, index of the item) in a dev posting that has other items too, so that losing this one must cost
    the score (losing a wrong item would raise it) and leaves the posting an answer worth keeping."""
    for found in score.load_gold("dev"):
        rows = rules_extract(found["description"])
        gold = [(g["start"], g["end"], g["importance"]) for g in found["requirements"]]
        pairs = score.match([(r.start, r.end, r.importance) for r in rows], gold)
        if pairs and len(rows) >= 2:
            return found["posting_id"], pairs[0][0]
    raise AssertionError("no dev posting has a matched item and another")


def test_a_degraded_recording_lowers_the_score_misses_the_baseline_and_names_the_posting(engine, tmp_path):
    clean = score.run("model", "dev")
    assert clean["metrics"]["bars_met"] is True and clean["metrics"]["rejected"] == {}
    posting_id, index = a_recorded_item_that_matches_a_label()

    def alter_one_quote(lines):
        target = next(line for line in lines if line["posting_id"] == posting_id)
        target["answers"][0]["requirements"][index]["quote"] += " (altered)"  # no longer words of the posting

    path = write_recordings(tmp_path, alter_one_quote)
    degraded = score.run("model", "dev", baseline=clean["metrics"], recordings=path)

    metrics = degraded["metrics"]
    assert metrics["req_item_f1"] < clean["metrics"]["req_item_f1"]
    assert metrics["rejected"] == {"quote_not_in_segments": 1}  # the validator turned the altered item away
    assert metrics["req_spans_resolved"] == 1.0  # it never stored an unresolved row; the loss is in the score
    assert metrics["bars"]["req_item_f1_not_below_baseline"] is False
    assert (metrics["bars_met"], metrics["score"]) == (False, 0.0)
    assert [i["key"] for i in degraded["items"] if not i["is_passed"]] == [posting_id]


def test_a_replay_of_the_dev_split_is_labelled_as_a_check_of_the_code_path(engine):
    metrics = score.run("model", "dev")["metrics"]
    assert metrics["harness_check_only"] is True
    assert "harness_check_only" not in score.run("rules", "dev")["metrics"]  # the rules run is a measurement
    assert the_fake_provider_is_back_to_its_own_recordings()


# ---- recording a run ----------------------------------------


def run_row(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT suite::text AS suite, git_sha, config, status::text AS status, metrics, is_gate_passed, "
                    "finished_at FROM eval_runs WHERE id = CAST(:id AS uuid)"
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
                    "WHERE eval_run_id = CAST(:id AS uuid) ORDER BY id"
                ),
                {"id": run_id},
            )
            .mappings()
            .all()
        )


def test_a_run_is_recorded_frozen_with_one_item_per_posting_and_a_config_that_says_what_it_is(engine):
    result = score.run("model", "dev")
    run_id = score.record(result, score.run_config("model", "dev"))
    run = run_row(engine, run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == ("requirement_extraction", "passed", True)
    assert run["finished_at"] is not None and run["git_sha"]
    assert run["metrics"] == result["metrics"]
    assert run["config"] == {
        "mode": "replay",
        "method": "model",
        "split": "dev",
        "labels": "agent-drafted, maintainer review pending",
        "min_iou": 0.5,
        "excluded": {},
        "task": "job.requirements.extract",
        "prompt_version": run["config"]["prompt_version"],
        "recording": "recordings.jsonl",
        "recording_source": "rules_fixture",
    }
    assert len(run["config"]["prompt_version"]) == 12
    items = item_rows(engine, run_id)
    assert [i["item_key"] for i in items] == [p["posting_id"] for p in score.load_gold("dev")]
    assert all(i["is_passed"] for i in items)
    assert items[0]["score"] == pytest.approx(result["items"][0]["score"], abs=1e-4)
    assert items[0]["expected"] == result["items"][0]["expected"]  # spans and importance, never the posting's words


def test_a_rules_run_is_recorded_as_code_with_no_model_named(engine):
    run_id = score.record(score.run("rules", "dev"), score.run_config("rules", "dev"))
    config_ = run_row(engine, run_id)["config"]
    assert (config_["mode"], config_["method"]) == ("code", "rules")
    assert not {"task", "prompt_version", "recording", "recording_source"} & set(config_)


def test_the_test_split_run_says_which_posting_it_left_out(engine):
    assert score.run_config("rules", "test")["excluded"] == {"req-049": score.EXCLUDED["req-049"]}


def test_the_command_line_records_runs_and_keeps_both_in_the_baseline_file(engine, tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    for method in ("rules", "model"):
        argv = ["--method", method, "--split", "dev", "--record", "--report", str(baseline)]
        assert score.main(argv) == 0  # recording a run is not an error; the gate decides
        out = capsys.readouterr().out.strip().splitlines()
        assert UUID(out[-1])  # the last line is the run id, which CI hands to the gate
    stored = json.loads(baseline.read_text(encoding="utf-8"))
    assert set(stored["runs"]) == {"rules", "replay"}
    assert "rules baseline, not a model quality number" in stored["note"]
    assert "labels drafted by an agent, review pending" in stored["note"]
    for key, method in (("rules", "rules"), ("replay", "model")):
        report = stored["runs"][key]
        assert report["config"]["method"] == method and report["labels"] == "labels drafted by an agent, review pending"
        assert report["note"].startswith("rules baseline, not a model quality number")
        assert report["metrics"] == run_row(engine, report["run_id"])["metrics"]  # published as the database holds it
        assert report["unclean_items"] == []
    assert (
        "recorded replay" in stored["runs"]["replay"]["note"]
        and "recorded replay" not in stored["runs"]["rules"]["note"]
    )


def test_the_command_line_prints_the_numbers_beside_the_review_note_and_exits_by_the_bars(engine, capsys):
    assert score.main(["--method", "rules", "--split", "dev"]) == 0
    out = capsys.readouterr().out
    assert "labels drafted by an agent, review pending" in out
    assert "rules baseline, not a model quality number" in out
    assert "req_item_f1" in out and "95% Wilson" in out and "req_spans_resolved 1.0000" in out and "MET" in out

    assert score.main(["--method", "model", "--split", "dev"]) == 0
    assert "proves the code path, not model quality" in capsys.readouterr().out


def test_a_failed_bar_is_exit_status_one_when_only_printing_and_the_run_is_still_recorded_with_record(
    engine, monkeypatch, capsys
):
    real = score.run("rules", "dev")
    failing = {"metrics": {**real["metrics"], "bars_met": False, "score": 0.0}, "items": real["items"]}
    monkeypatch.setattr(score, "run", lambda *args, **kwargs: failing)
    assert score.main(["--method", "rules", "--split", "dev"]) == 1
    capsys.readouterr()
    assert score.main(["--method", "rules", "--split", "dev", "--record"]) == 0
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status::text FROM eval_runs")).scalar_one() == "failed"


def test_recording_needs_a_database_named_on_purpose_and_a_report_needs_a_recording(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL_WORKER", raising=False)  # its default is the live database
    for argv in (["--method", "rules", "--record"], ["--method", "model", "--split", "dev"]):
        with pytest.raises(SystemExit) as refused:
            score.main(argv)
        assert refused.value.code == 2
    monkeypatch.setenv("DATABASE_URL_WORKER", "postgresql://example.invalid/x_test")
    with pytest.raises(SystemExit) as refused:
        score.main(["--method", "rules", "--report", str(tmp_path / "baseline.json")])
    assert refused.value.code == 2


def test_a_replay_refuses_a_real_provider(engine, monkeypatch):
    real = replace(config.get(), llm_provider="anthropic")
    monkeypatch.setattr(config, "get", lambda: real)
    with pytest.raises(SystemExit) as refused:
        score.main(["--method", "model", "--split", "dev"])
    assert refused.value.code == 2


def test_a_broken_recording_is_exit_status_two_and_names_the_remedy(engine, monkeypatch, capsys, tmp_path):
    path = write_recordings(tmp_path, lambda lines: lines[0].update(request_sha256="0" * 64))
    real_run = score.run
    monkeypatch.setattr(score, "run", lambda method, split, baseline=None: real_run(method, split, baseline, path))
    assert score.main(["--method", "model", "--split", "dev"]) == 2
    assert "record_fixture.py" in capsys.readouterr().err


# ---- baseline.json, as committed ----------------------------------------


def stored_run(key):
    return json.loads((SUITE_DIR / "baseline.json").read_text(encoding="utf-8"))["runs"][key]


def numbers(metrics):
    return {name: value for name, value in metrics.items() if name not in VOLATILE}


def test_the_replay_of_the_committed_recordings_reproduces_the_stored_replay_exactly(engine):
    """What the CI gate replays. Deterministic, so the same recordings through the same code give the same numbers."""
    replayed = score.run("model", "test")["metrics"]
    assert numbers(replayed) == numbers(stored_run("replay")["metrics"])


def test_the_rules_run_reproduces_the_stored_rules_baseline_exactly():
    assert numbers(score.run("rules", "test")["metrics"]) == numbers(stored_run("rules")["metrics"])


def test_a_replay_that_equals_the_stored_baseline_meets_the_bars_the_gate_applies(engine):
    held = score.run("model", "test", baseline=stored_run("replay")["metrics"])["metrics"]
    assert held["bars"] == {
        "req_spans_resolved": True,
        "req_item_f1_not_below_baseline": True,
        "req_importance_accuracy_not_below_baseline": True,
    }
    assert (held["bars_met"], held["score"]) == (True, 1.0)


def test_the_stored_runs_are_on_the_clean_test_split_and_say_they_are_baselines_and_not_model_quality():
    stored = json.loads((SUITE_DIR / "baseline.json").read_text(encoding="utf-8"))
    assert set(stored["runs"]) == {"replay", "rules"}
    assert "rules baseline, not a model quality number" in stored["note"]
    assert "labels drafted by an agent, review pending" in stored["note"]
    for key, method in (("replay", "model"), ("rules", "rules")):
        run = stored["runs"][key]
        assert run["suite"] == "requirement_extraction" and run["status"] == "passed" and run["git_sha"]
        assert run["note"].startswith("rules baseline, not a model quality number")
        assert run["labels"] == "labels drafted by an agent, review pending"
        assert run["config"]["method"] == method and run["config"]["split"] == "test"
        assert run["config"]["labels"] == "agent-drafted, maintainer review pending"
        assert set(run["config"]["excluded"]) == {"req-049"}
        metrics = run["metrics"]
        assert (metrics["n"], metrics["gold_items"]) == (39, 390)  # the clean test split
        assert metrics["req_spans_resolved"] == 1.0 and run["unclean_items"] == []
    replay = stored["runs"]["replay"]
    assert replay["config"]["recording_source"] == "rules_fixture" and replay["metrics"]["harness_check_only"] is True
    assert "proves the code path, not model quality" in replay["note"]
    assert "harness_check_only" not in stored["runs"]["rules"]["metrics"]  # a real measurement, of the baseline


# ---- the documentation of the bars ----------------------------------------


def test_the_bars_were_written_down_with_a_date_before_any_number_and_say_what_they_do_not_show():
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    bars = readme.split("## Pre-registered bars")[1]
    assert "Written on 2026-11-23, before any run of `score.py`" in bars
    assert (
        "`req_spans_resolved = 1.0`" in bars
        and "`req_item_f1 >= 0.85`" in bars
        and "`req_importance_accuracy >= 0.90`" in bars
    )
    assert "req-049" in bars and "labels drafted by an agent, review pending" in bars
    assert "they are baselines" in bars and "No bar for the rules method" in bars
