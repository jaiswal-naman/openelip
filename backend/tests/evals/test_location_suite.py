"""IDX-06: the location_accuracy suite scores the parser honestly, records a frozen run, and the gate agrees.

Every test scores temporary samples written here, or the dev split. None scores the test split of
evals/location/sample.jsonl: that is scored once, by the recorded run (evals/location/baseline.json). Two tests read its
ids and input strings, never its labels or the parser's answers, to check which rows EXCLUDED leaves out.
"""

import ast
import importlib.util
import json
import re
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.evals import EVALS, gate

SCORE_FILE = EVALS / "location" / "score.py"
spec = importlib.util.spec_from_file_location("location_score", SCORE_FILE)
assert spec and spec.loader
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)

# The city names the tests below use, searched for in GeoNames once instead of once per test.
NAMES = {"Berlin", "Tokyo", "Osaka", "Pune", "Bangalore", "Bengaluru", "New York", "New York City", "San Francisco"}


@pytest.fixture(scope="module", autouse=True)
def city_names_looked_up_once():
    suite.city_ids(NAMES)


def make_row(n, raw, label_country, label_city, hint=None, split="test"):
    return {
        "id": f"loc-{n:03d}",
        "split": split,
        "location_raw": raw,
        "country_hint": hint,
        "remote_hint": None,
        "label_country": label_country,
        "label_city": label_city,
    }


def write_sample(tmp_path, rows) -> Path:
    path = tmp_path / "sample.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


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
                    "SELECT item_key, slice, expected, actual, is_passed, score FROM eval_items "
                    "WHERE eval_run_id = CAST(:id AS uuid) ORDER BY id"
                ),
                {"id": run_id},
            )
            .mappings()
            .all()
        )


# ---- how a row is judged ----------------------------------------


def test_a_bengaluru_prediction_counts_as_correct_for_a_bangalore_label():
    rows = [make_row(1, "Bengaluru, India", "IN", "Bangalore"), make_row(2, "New York, NY", "US", "New York")]
    result = suite.score(rows)
    assert [item["is_passed"] for item in result["items"]] == [True, True]
    assert result["items"][0]["actual"] == {"country": "IN", "city": "Bengaluru"}  # the parser's own spelling is kept


def test_a_wrong_city_in_the_right_country_is_wrong_and_is_not_a_guess():
    result = suite.score([make_row(1, "Tokyo", "JP", "Osaka")])
    assert result["items"][0]["is_passed"] is False and result["metrics"]["location_guessed"] == 0


def test_none_against_none_is_correct_and_a_value_against_a_null_label_is_one_guess():
    rows = [make_row(1, "HQ", None, None), make_row(2, "Berlin", None, None)]
    result = suite.score(rows)
    assert [item["is_passed"] for item in result["items"]] == [True, False]
    assert [item["guessed"] for item in result["items"]] == [False, True]
    assert result["metrics"]["location_guessed"] == 1


def test_a_predicted_country_against_a_null_label_is_one_guess_whatever_it_predicts_for_the_city():
    result = suite.score([make_row(1, "anything", None, None)], parse=lambda *_: ("IN", None, None))
    assert result["items"][0]["is_passed"] is False and result["metrics"]["location_guessed"] == 1


def test_a_city_where_the_label_has_only_a_country_is_a_guess():
    result = suite.score([make_row(1, "San Francisco", "US", None)])  # the label says: a country and no city
    assert result["items"][0]["guessed"] is True and result["items"][0]["is_passed"] is False


def test_a_missing_prediction_for_a_labelled_place_is_wrong_but_not_a_guess():
    result = suite.score([make_row(1, "HQ", "DE", "Berlin")])
    assert result["items"][0]["is_passed"] is False and result["items"][0]["guessed"] is False


def test_each_item_is_sliced_by_what_its_label_says():
    rows = [
        make_row(1, "HQ", None, None),
        make_row(2, "Remote - USA", "US", None),
        make_row(3, "Berlin", "DE", "Berlin"),
        make_row(4, "Cambridge", None, "Cambridge"),
    ]
    assert [item["slice"] for item in suite.score(rows)["items"]] == ["unknown", "country", "country_and_city", "city"]


# ---- the numbers and the bars ----------------------------------------


def test_three_rows_with_two_correct_report_the_counts_the_accuracy_and_a_wilson_interval(tmp_path, capsys):
    rows = [
        make_row(1, "Pune, India", "IN", "Pune"),
        make_row(2, "Berlin", "DE", "Berlin"),
        make_row(3, "Tokyo", "JP", "Osaka"),
    ]
    metrics = suite.score(rows)["metrics"]
    assert (metrics["n"], metrics["correct"], metrics["location_accuracy"]) == (3, 2, 0.6667)
    assert metrics["wilson_low"] < 0.6667 < metrics["wilson_high"] and 0 < metrics["wilson_low"]
    assert metrics["bars_met"] is False
    assert suite.main(["--sample", str(write_sample(tmp_path, rows))]) == 1  # the command line says so too
    out = capsys.readouterr().out
    assert "2 of 3 rows correct" in out and "NOT MET" in out and "wrong: loc-003" in out
    assert "labels drafted by an agent, review pending" in out


def test_the_wilson_interval_matches_known_values():
    assert suite.wilson(0, 0) == (0.0, 1.0)
    low, high = suite.wilson(6, 6)
    assert 0.60 < low < 0.62 and high == pytest.approx(1.0)
    low, high = suite.wilson(0, 6)
    assert low == 0.0 and 0.38 < high < 0.40


def accurate_rows(wrong=0, guessed=0):
    """Twenty rows: `wrong` rows that are wrong without guessing, `guessed` rows that guess, the rest correct."""
    rows = [make_row(i, "Berlin", "DE", "Berlin") for i in range(1, 21)]
    for i in range(wrong):
        rows[i] = make_row(i + 1, "Tokyo", "JP", "Osaka")
    for i in range(guessed):
        rows[wrong + i] = make_row(wrong + i + 1, "Berlin", None, None)
    return rows


def test_the_bars_are_met_at_exactly_095_and_no_guess():
    metrics = suite.score(accurate_rows(wrong=1))["metrics"]  # 19 of 20
    assert metrics["location_accuracy"] == 0.95 and metrics["bars_met"] is True and metrics["score"] == 0.95


def test_the_suite_fails_below_095():
    metrics = suite.score(accurate_rows(wrong=2))["metrics"]  # 18 of 20
    assert metrics["bars_met"] is False and metrics["location_guessed"] == 0


def test_the_suite_fails_on_a_single_guess_however_accurate_it_is_otherwise():
    metrics = suite.score(accurate_rows(guessed=1))["metrics"]  # 19 of 20 correct, and one of the misses a guess
    assert metrics["location_accuracy"] == 0.95 and metrics["location_guessed"] == 1
    assert metrics["bars_met"] is False and metrics["score"] == 0.0  # the gate sees a zero


def test_an_empty_sample_meets_no_bar():
    assert suite.score([])["metrics"]["bars_met"] is False


def test_the_null_labels_are_counted_so_a_report_shows_how_few_rows_could_expose_a_guess():
    rows = [
        make_row(1, "HQ", None, None),
        make_row(2, "Remote - USA", "US", None),
        make_row(3, "Berlin", "DE", "Berlin"),
    ]
    assert suite.score(rows)["metrics"]["null_labels"] == {"country": 1, "city": 2, "both": 1}


def test_load_sample_keeps_one_split_and_refuses_a_row_that_lacks_a_field(tmp_path):
    rows = [make_row(1, "Berlin", "DE", "Berlin", split="dev"), make_row(2, "Tokyo", "JP", "Tokyo", split="test")]
    path = write_sample(tmp_path, rows)
    assert [row["id"] for row in suite.load_sample(path, "test")] == ["loc-002"]
    path.write_text(json.dumps({"id": "loc-009", "split": "test"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="lacks"):
        suite.load_sample(path, "test")


def test_the_dev_split_meets_the_bars_the_parser_was_built_on():
    result = suite.score(suite.load_sample(suite.SAMPLE, "dev"))
    metrics = result["metrics"]
    assert metrics["n"] == 100 and metrics["location_guessed"] == 0
    assert metrics["bars_met"] is True, [i["key"] for i in result["items"] if not i["is_passed"]]


# ---- which test rows are unseen data ----------------------------------------

PARSER_TESTS = EVALS.parent / "backend" / "tests" / "ingest" / "test_location.py"
GUIDELINE = EVALS / "location" / "GUIDELINE.md"


def committed_rows():
    return [json.loads(line) for line in suite.SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]


def written_out():
    """Every string the parser's unit tests spell out, and every string the guideline quotes or sets in a table cell."""
    tree = ast.parse(PARSER_TESTS.read_text(encoding="utf-8"))
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    guideline = GUIDELINE.read_text(encoding="utf-8")
    strings |= set(re.findall(r'"([^"\n]+)"', guideline)) | set(re.findall(r"`([^`\n]+)`", guideline))
    return strings | {cell.strip() for cell in re.findall(r"^\|\s*([^|]+?)\s*\|", guideline, flags=re.MULTILINE)}


def test_the_excluded_rows_are_exactly_the_test_rows_whose_input_is_written_out_in_the_tests_or_the_guideline():
    """The test split is frozen: its inputs are not to be read before the run. 40 of its rows have an input that the
    parser's unit tests or the guideline spell out (and that no dev row carries, so that it is not a dev input either).
    Quote another test input in either file and this fails until the row is listed in EXCLUDED and the run is redone."""
    rows = committed_rows()
    dev_inputs = {row["location_raw"] for row in rows if row["split"] == "dev"}
    quoted = written_out()
    leaked = {
        row["id"]
        for row in rows
        if row["split"] == "test" and row["location_raw"] in quoted and row["location_raw"] not in dev_inputs
    }
    assert len(suite.EXCLUDED) == 40
    assert leaked == suite.EXCLUDED


def test_the_test_score_leaves_the_excluded_rows_out_and_the_dev_split_keeps_all_of_its_rows():
    rows = committed_rows()
    test_ids = {row["id"] for row in rows if row["split"] == "test"}
    assert len(test_ids) == 300 and suite.EXCLUDED <= test_ids  # they stay in sample.jsonl, and only in the test split
    scored = suite.load_sample(suite.SAMPLE, "test")
    assert len(scored) == 260 and not {row["id"] for row in scored} & suite.EXCLUDED
    assert len(suite.load_sample(suite.SAMPLE, "dev")) == 100


def test_a_run_says_which_rows_it_left_out_and_why():
    left_out = suite.run_config("test", SCORE_FILE)["excluded"]
    assert left_out == {"why": suite.EXCLUDED_WHY, "ids": sorted(suite.EXCLUDED)}
    assert suite.run_config("dev", SCORE_FILE)["excluded"] == {}


# ---- the recorded run ----------------------------------------


def test_a_passing_run_is_recorded_frozen_with_one_item_per_row_and_the_gate_passes_it(engine, capsys):
    rows = [
        make_row(1, "Berlin", "DE", "Berlin"),
        make_row(2, "Pune, India", "IN", "Pune"),
        make_row(3, "HQ", None, None),
    ]
    run_id = suite.record(suite.score(rows), suite.run_config("test", SCORE_FILE))
    run = run_row(engine, run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == ("location_accuracy", "passed", True)
    assert run["finished_at"] is not None and run["git_sha"]
    metrics = run["metrics"]
    assert (metrics["n"], metrics["correct"], metrics["passed"], metrics["score"]) == (3, 3, 3, 1.0)
    assert (metrics["location_accuracy"], metrics["location_guessed"], metrics["bars_met"]) == (1.0, 0, True)
    assert re.fullmatch(r"[0-9a-f]{64}", run["config"]["parser_sha256"])
    assert run["config"]["labels"] == "agent-drafted, maintainer review pending" and run["config"]["mode"] == "code"
    items = item_rows(engine, run_id)
    assert [item["item_key"] for item in items] == ["loc-001", "loc-002", "loc-003"]
    assert [item["slice"] for item in items] == ["country_and_city", "country_and_city", "unknown"]
    assert items[1]["expected"] == {"country": "IN", "city": "Pune"} and items[1]["actual"] == items[1]["expected"]
    assert all(item["is_passed"] and item["score"] == 1.0 for item in items)
    assert gate.main("location_accuracy", UUID(run_id)) == 0  # the floor is in evals/thresholds.toml
    assert "scored 1.000 (3 of 3 items), floor 0.950: PASS" in capsys.readouterr().out


def test_a_run_below_the_bar_is_recorded_as_failed_and_the_gate_names_the_wrong_rows(engine, capsys):
    rows = [make_row(1, "Berlin", "DE", "Berlin"), make_row(2, "Tokyo", "JP", "Osaka")]
    run_id = suite.record(suite.score(rows), suite.run_config("test", SCORE_FILE))
    run = run_row(engine, run_id)
    assert (run["status"], run["is_gate_passed"], run["metrics"]["score"]) == ("failed", False, 0.5)
    assert gate.main("location_accuracy", UUID(run_id)) == 1
    assert "items that failed: loc-002" in capsys.readouterr().out


def test_a_run_with_a_guess_fails_the_gate_although_its_accuracy_is_at_the_floor(engine, capsys):
    run_id = suite.record(suite.score(accurate_rows(guessed=1)), suite.run_config("test", SCORE_FILE))
    run = run_row(engine, run_id)
    assert (run["status"], run["is_gate_passed"]) == ("failed", False)
    assert run["metrics"]["location_accuracy"] == 0.95 and run["metrics"]["location_guessed"] == 1
    assert gate.main("location_accuracy", UUID(run_id)) == 1
    assert "FAIL" in capsys.readouterr().out


def test_a_finished_run_cannot_be_changed(engine):
    run_id = suite.record(suite.score([make_row(1, "Berlin", "DE", "Berlin")]), suite.run_config("test", SCORE_FILE))
    with pytest.raises(DBAPIError, match="finished and cannot change"), engine.begin() as conn:
        conn.execute(text("UPDATE eval_runs SET is_gate_passed = false WHERE id = CAST(:id AS uuid)"), {"id": run_id})


def test_the_command_line_records_and_publishes_the_run_it_recorded(engine, tmp_path, capsys):
    rows = [make_row(1, "Berlin", "DE", "Berlin"), make_row(2, "Tokyo", "JP", "Osaka")]
    report_path = tmp_path / "baseline.json"
    argv = ["--sample", str(write_sample(tmp_path, rows)), "--record", "--report", str(report_path)]
    assert suite.main(argv) == 1  # 0.5 is under the bar, and the run is recorded all the same
    report = json.loads(report_path.read_text(encoding="utf-8"))
    run = run_row(engine, report["run_id"])
    assert report["status"] == run["status"] == "failed" and report["metrics"] == run["metrics"]
    assert report["config"] == run["config"] and report["wrong_items"] == ["loc-002"]
    assert report["labels"] == "labels drafted by an agent, review pending"
    assert f"recorded as run {report['run_id']}" in capsys.readouterr().out


def test_recording_needs_a_database_named_on_purpose_and_a_report_needs_a_recording(monkeypatch, tmp_path):
    sample = str(write_sample(tmp_path, [make_row(1, "Berlin", "DE", "Berlin")]))
    monkeypatch.delenv("DATABASE_URL_WORKER", raising=False)  # its default is the live database
    with pytest.raises(SystemExit) as refused:
        suite.main(["--sample", sample, "--record"])
    assert refused.value.code == 2
    with pytest.raises(SystemExit) as refused:
        suite.main(["--sample", sample, "--report", str(tmp_path / "x.json")])
    assert refused.value.code == 2


def worker_at(monkeypatch, name):
    monkeypatch.setenv("DATABASE_URL_WORKER", f"postgresql://app_worker:placeholder@127.0.0.1:5433/{name}")


@pytest.mark.parametrize("name", ["app", "postgres", "app_test_backup"])
def test_recording_to_a_database_that_is_not_a_scratch_one_is_refused_before_anything_is_written(
    monkeypatch, tmp_path, capsys, name
):
    worker_at(monkeypatch, name)
    monkeypatch.setattr(suite, "record", lambda *args: pytest.fail("a run was written"))
    sample = str(write_sample(tmp_path, [make_row(1, "Berlin", "DE", "Berlin")]))
    with pytest.raises(SystemExit) as refused:
        suite.main(["--sample", sample, "--record"])
    assert refused.value.code == 2 and repr(name) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "flags"), [("app", ["--live"]), ("evals", []), ("app_p2_fix_run_test", [])], ids=["live", "evals", "_test"]
)
def test_a_scratch_database_or_live_lets_the_run_be_recorded(monkeypatch, tmp_path, capsys, name, flags):
    worker_at(monkeypatch, name)
    monkeypatch.setattr(suite, "record", lambda result, config: "run-1")
    sample = str(write_sample(tmp_path, [make_row(1, "Berlin", "DE", "Berlin")]))
    assert suite.main(["--sample", sample, "--record", *flags]) == 0
    assert "recorded as run run-1" in capsys.readouterr().out


def test_the_published_baseline_is_a_run_on_the_scored_rows_and_names_the_rows_it_left_out():
    stored = json.loads((EVALS / "location" / "baseline.json").read_text(encoding="utf-8"))
    assert stored["config"]["split"] == "test" and stored["labels"] == "labels drafted by an agent, review pending"
    assert stored["config"]["excluded"] == suite.run_config("test", SCORE_FILE)["excluded"]
    assert stored["metrics"]["n"] == len(suite.load_sample(suite.SAMPLE, "test"))
    assert stored["metrics"]["location_guessed"] == 0 and stored["status"] == "passed"


def test_the_floor_and_the_documentation_are_in_place():
    thresholds = (EVALS / "thresholds.toml").read_text(encoding="utf-8")
    assert "[location_accuracy]" in thresholds and "min_score = 0.95" in thresholds
    readme = (EVALS / "location" / "README.md").read_text(encoding="utf-8")
    assert "## Pre-registered bars" in readme and "location_guessed == 0" in readme
    assert "labels drafted by an agent, review pending" in readme
    left_out = readme.split("## Rows left out of the test score")[1].split("## Pre-registered bars")[0]
    assert "labels drafted by an agent, review pending" in left_out and "40 test rows" in left_out
    ai_system = (EVALS.parent / "docs" / "AI_SYSTEM.md").read_text(encoding="utf-8")
    assert "`location_accuracy`, `location_guessed`" in ai_system
