"""EVL-01 resume leg: the invented gold set runs through the Phase 1 eval gate in replay (plan 03-03, task 2).

The replay uses recordings a script wrote, not a model, so what is checked here is the code path and the gate: a clean
set passes the floor taken from baseline.json, a degraded set fails it, a stale recording stops the run. None of these
numbers says how good any model is (invariant 8). The runs go through the real gateway and the fake provider, on a
database only this module uses (every call leaves a ledger row, and other modules truncate the shared one).
"""

import importlib.util
import json
import os
import random
import sys
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.evals import EVALS, gate, runner
from app.evals.__main__ import main
from app.platform import config, db
from app.platform.llm import complete
from app.platform.llm import fake as provider
from app.profile.extract import fake, workflow
from app.profile.extract.fake import RecordingStale, result_from_lines
from app.profile.extract.schemas import CandidateFact
from app.profile.extract.workflow import extract_facts

FACTS = EVALS / "resume_extraction" / "facts"
DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")
ROLES, EDUCATION_SKILLS, SUMMARY = (task.name for task in workflow.TASKS)


def load_script(name):
    """A script under evals/resume_extraction/facts, loaded by path: evals/ is data and scripts, not a package."""
    spec = importlib.util.spec_from_file_location(f"facts_{Path(name).stem}", FACTS / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def gold_items():
    return [json.loads(line) for line in (FACTS / "gold.jsonl").read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def gold_db(make_database, alembic_run):
    url = make_database(f"app_p3_03_gold_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def gold_engine(gold_db, monkeypatch):
    """A superuser engine on this module's database, emptied for each test; the application's settings and engines
    point at the same database with the fake provider chosen."""
    admin = create_engine(gold_db.set(drivername="postgresql+psycopg"))
    with admin.begin() as conn:
        conn.execute(text("TRUNCATE eval_runs, eval_items, llm_calls, model_registry RESTART IDENTITY CASCADE"))
    urls = {
        name: make_url(os.environ[name]).set(database=gold_db.database).render_as_string(hide_password=False)
        for name in DB_VARIABLES
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    yield admin
    for application_engine in db._engines.values():
        application_engine.dispose()
    provider.replay_from(None)
    admin.dispose()


def one(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().one()


def run_row(engine, run_id):
    columns = "suite::text AS suite, config, status::text AS status, metrics, is_gate_passed"
    return one(engine, f"SELECT {columns} FROM eval_runs WHERE id = :id", id=run_id)


# --- the gold set ------------------------------------------------------------------------------------------------


def test_the_generator_writes_the_same_120_invented_resumes_every_time():
    generate = load_script("generate.py")
    first = json.loads(json.dumps(generate.build(7)))
    assert first == json.loads(json.dumps(generate.build(7))) == gold_items()  # the committed file is this seed's
    assert Counter(item["slice"] for item in first) == {"single": 60, "two_column": 30, "sidebar": 30}
    assert len({item["key"] for item in first}) == 120 and all(item["synthetic"] is True for item in first)
    assert generate.build(8) != first


def test_the_gold_set_is_invented_and_says_so():
    text_of_all = "\n".join("\n".join(item["lines"]) for item in gold_items())
    emails = {word for word in text_of_all.replace("|", " ").split() if "@" in word}
    assert emails and all(email.endswith("@example.com") for email in emails)
    readme = (FACTS / "README.md").read_text(encoding="utf-8")
    assert "invented" in readme and "not a model quality number" in readme and "invariant 8" in readme


def test_a_flawless_reader_scores_one_on_every_resume_so_the_truth_and_the_checks_agree():
    score, record = load_script("score.py"), load_script("record_fake.py")
    for item in score.load_gold():
        answers = record.answers(item, random.Random(0), rate=0.0)

        def gateway(task, schema, messages, *, principal, prompt_version, answers=answers):
            return schema.model_validate(answers[task])

        out = extract_facts(gateway, result_from_lines(item["lines"]), attribution=None)
        _, fp, fn = score.tally(item, out.kept)["all"]
        assert (fp, fn) == (0, 0), item["key"]
        assert out.unverified == [] and out.failed_calls == []
        assert out.protected_skipped == len(item["protected_lines"])  # the careless extra item was declined


def test_the_scorer_counts_a_wrong_field_a_missed_fact_and_a_stray_fact():
    score = load_script("score.py")
    truth = {"kind": "role", "line_start": 1, "line_end": 2, "employer": "Globex", "title": "Engineer",
             "start_date": "2020", "end_date": "2021"}  # fmt: skip
    item = {"lines": ["Engineer, Globex", "2020 - 2021"], "expected": {"facts": [truth]}}
    attributes = {"employer": "Globex", "title": "Engineer", "start_date": "2020", "end_date": "2021"}
    right = CandidateFact("role", "", attributes, None, (0, 28), "")
    wrong = replace(right, attributes={**attributes, "employer": "Initech"})
    stray = CandidateFact("achievement", "", {}, None, (100, 110), "")
    assert score.tally(item, [right]) == {"all": [5, 0, 0], "group": [4, 0, 0]}  # kind and four fields
    assert score.tally(item, [wrong]) == {"all": [4, 1, 1], "group": [3, 1, 1]}  # a wrong value is both errors
    assert score.tally(item, []) == {"all": [0, 0, 5], "group": [0, 0, 4]}  # nothing kept: every field missed
    assert score.tally(item, [right, stray])["all"] == [
        5,
        1,
        0,
    ]  # a fact with no truth counts once, as a false positive
    assert score.f1(4, 1, 1) == 0.8 and score.f1(0, 3, 3) == 0.0 and score.f1(5, 0, 0) == 1.0


def test_recordings_are_keyed_by_the_request_so_a_changed_prompt_makes_them_stale(monkeypatch):
    item = gold_items()[0]
    recordings = fake.load(FACTS / "recordings.jsonl")

    def hashes():
        requests = workflow.build_requests(result_from_lines(item["lines"]))
        return [fake.request_hash(task.name, messages) for task, messages in requests]

    assert all(known in recordings for known in hashes())  # the committed recordings answer today's requests
    edited = tuple(replace(task, prompt=task.prompt + " One more sentence.") for task in workflow.TASKS)
    monkeypatch.setattr(workflow, "TASKS", edited)
    assert not any(known in recordings for known in hashes())  # a prompt edit changes every request: all stale


# --- the suite through the Phase 1 gate ----------------------------------------------------------------------------


def test_the_replay_goes_through_the_real_gateway_equals_the_baseline_and_passes_the_gate(gold_engine, capsys):
    baseline = json.loads((FACTS / "baseline.json").read_text(encoding="utf-8"))
    assert gate.floor("resume_extraction") == baseline["score"]  # the floor is the baseline, minus 0.0
    done = runner.run_suite("resume_extraction")
    run = run_row(gold_engine, done.run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == ("resume_extraction", "passed", True)
    assert run["config"]["mode"] == "replay" and run["config"]["provider"] == "fake"
    assert run["config"]["prompt_versions"] == workflow.prompt_versions() and run["config"]["synthetic"] is True
    expected = {key: value for key, value in baseline.items() if key != "note"}
    assert {key: run["metrics"][key] for key in expected} == expected  # deterministic: equal to the recorded baseline
    assert run["metrics"]["quote_exact_share"] == 1.0 and run["metrics"]["replay_only"] is True
    assert run["metrics"]["unverified"] > 0 and run["metrics"]["protected_skipped"] > 0  # faults caught, lines declined
    assert one(gold_engine, "SELECT count(*) AS n FROM eval_runs")["n"] == 1
    assert one(gold_engine, "SELECT count(*) AS n FROM eval_items WHERE eval_run_id = :id", id=done.run_id)["n"] == 120
    assert gate.main("resume_extraction", done.run_id) == 0
    assert "PASS" in capsys.readouterr().out
    with gold_engine.connect() as conn:
        ledger = Counter(tuple(row) for row in conn.execute(text("SELECT task, outcome::text FROM llm_calls")))
    assert ledger == {(ROLES, "ok"): 120, (EDUCATION_SKILLS, "ok"): 120, (SUMMARY, "ok"): 120}


def test_a_degraded_recording_set_scores_below_the_floor_and_the_gate_fails(gold_engine, tmp_path, capsys):
    degraded = tmp_path / "recordings_degraded.jsonl"
    record = load_script("record_fake.py")
    record.write(record.record(gold_items(), rate=0.35), degraded)
    done = runner.run_suite("resume_extraction", degraded)
    run = run_row(gold_engine, done.run_id)
    assert (run["status"], run["is_gate_passed"], run["config"]["recorded"]) == ("failed", False, degraded.name)
    assert run["metrics"]["score"] < gate.floor("resume_extraction")
    assert gate.main("resume_extraction", done.run_id) == 1
    assert "FAIL" in capsys.readouterr().out


def test_a_recording_that_no_longer_matches_its_request_ends_the_run_in_error(gold_engine, tmp_path):
    lines = (FACTS / "recordings.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[5])
    lines[5] = json.dumps({**entry, "request": "0" * 64})  # as if the prompt, the masking or the resume had changed
    stale = tmp_path / "stale.jsonl"
    stale.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = runner.run_suite("resume_extraction", stale)
    run = run_row(gold_engine, done.run_id)
    assert (done.status, run["status"], run["is_gate_passed"]) == ("error", "error", None)
    assert run["metrics"]["error_code"] == "RecordingStale"
    assert gate.main("resume_extraction", done.run_id) == 1


def test_the_command_line_runs_the_committed_recordings_and_the_gate_exits_zero(gold_engine, capsys):
    assert main(["run", "resume_extraction"]) == 0
    run_id = capsys.readouterr().out.strip().splitlines()[-1]  # the last line of standard output is the run id
    assert uuid.UUID(run_id)
    assert main(["gate", "resume_extraction", "--run", run_id]) == 0


def test_a_replay_of_this_suite_refuses_a_real_provider_and_writes_nothing(gold_engine, monkeypatch):
    real = replace(config.get(), llm_provider="anthropic")
    monkeypatch.setattr(config, "get", lambda: real)
    with pytest.raises(runner.ReplayNeedsFake):
        runner.run_suite("resume_extraction")
    assert one(gold_engine, "SELECT count(*) AS n FROM eval_runs")["n"] == 0


def test_a_kept_fact_whose_quote_is_not_the_text_at_its_span_ends_the_run_in_error(gold_engine, monkeypatch):
    real = workflow.extract_facts

    def lying(gateway, result, **options):
        out = real(gateway, result, **options)
        first, *rest = out.kept
        return replace(out, kept=[replace(first, source_quote=first.source_quote + "!"), *rest])

    monkeypatch.setattr(workflow, "extract_facts", lying)
    done = runner.run_suite("resume_extraction")
    run = run_row(gold_engine, done.run_id)
    assert (run["status"], run["is_gate_passed"], run["metrics"]["error_code"]) == ("error", None, "QuoteMismatch")
    assert gate.main("resume_extraction", done.run_id) == 1


def test_a_stale_recording_names_the_task_and_never_the_resume():
    item = gold_items()[0]
    requests = [(task.name, messages) for task, messages in workflow.build_requests(result_from_lines(item["lines"]))]
    with pytest.raises(RecordingStale) as stale:
        fake.replay_file(requests, {}, Path("unused.json"))
    assert str(stale.value) == ROLES and item["lines"][0] not in str(stale.value)


def test_one_request_recorded_twice_is_refused(tmp_path):
    line = (FACTS / "recordings.jsonl").read_text(encoding="utf-8").splitlines()[0]
    twice = tmp_path / "twice.jsonl"
    twice.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        fake.load(twice)


# --- the real gateway with hand-written answers ---------------------------------------------------------------------


def test_the_real_gateway_and_fake_provider_with_hand_written_answers(gold_engine, tmp_path):
    result = result_from_lines(["Data Engineer, Veltrix Labs", "Mar 2021 - Present", "Skills: Python, SQL"])
    answers = [
        "not json at all",  # the roles call: invalid output, asked again ...
        {"items": [{"kind": "oops"}]},  # ... and invalid again, so the call is reported as failed
        {"items": [{"kind": "skill", "line_start": 3, "line_end": 3, "institution": None, "degree": None,
                    "name": "SQL", "start_date": None, "end_date": None}]},
        {"items": []},
    ]  # fmt: skip
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(answers), encoding="utf-8")
    provider.replay_from(path)
    out = extract_facts(complete, result, attribution=None, parallel=False)
    assert out.failed_calls == [ROLES]
    assert [(f.kind, f.statement) for f in out.kept] == [("skill", "SQL")]
    with gold_engine.connect() as conn:
        rows = [
            tuple(row) for row in conn.execute(text("SELECT task, outcome::text, attempt FROM llm_calls ORDER BY id"))
        ]
    assert rows == [
        (ROLES, "schema_invalid", 1),
        (ROLES, "schema_invalid", 2),
        (EDUCATION_SKILLS, "ok", 1),
        (SUMMARY, "ok", 1),
    ]
