"""A real model reads a resume whether or not a gold-set run has measured it (AI_SYSTEM 2.2; TRU-05).

models.toml names claude-haiku-5-5 (and a local placeholder) for the three resume.facts tasks. The gold set measures
accuracy, it does not decide whether a model may be used, so a key alone is enough: the upload and the parse go on. What
still means "AI is off" is a route that cannot be called (no route, no key, an unpriced model, the fake in production).
A real model with no passed resume_extraction run on the test split, by the model method, naming the model and the
prompts in use, is written to the log as one info line (status "unproven"); a measured one writes nothing. Nothing here
calls a model: the check only reads routing and runs.
"""

import json
import logging
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform import config
from app.platform.llm import AIDisabled, registry
from app.profile import ai_gate
from app.profile.extract import workflow

URL = "/v1/me/documents"
VERSIONS = workflow.prompt_versions()
KEYED = replace(config.get(), llm_provider="anthropic", anthropic_api_key="k", openai_api_key=None, llm_base_url=None)
ROUTE_MODEL = registry.route(workflow.TASKS[0].name, KEYED).model  # what models.toml says answers the tasks


def proof(**over):
    """The config of a run that proves the routes: the model method on the test split, naming the model the routes use
    and the prompt versions in use."""
    return {"method": "model", "split": "test", "model": ROUTE_MODEL, "prompt_versions": VERSIONS, **over}


def record_run(conn, run_config, *, suite="resume_extraction", status="passed"):
    """A finished run in eval_runs, inserted as it ends: no later update, which the table would refuse."""
    conn.execute(
        text(
            """INSERT INTO eval_runs (suite, git_sha, config, status, metrics, is_gate_passed, finished_at)
               VALUES (CAST(:suite AS eval_suite), 'abc', CAST(:config AS jsonb), CAST(:status AS eval_status),
                       CAST('{}' AS jsonb), :ok, now())"""
        ),
        {"suite": suite, "config": json.dumps(run_config), "status": status, "ok": status == "passed"},
    )


def reason_of(conn):
    """Why the gate is closed, or None when it is open."""
    try:
        ai_gate.require_available(conn)
    except AIDisabled as closed:
        return closed.reason
    return None


@pytest.fixture
def keyed(monkeypatch):
    """A real model with a key. Nothing is called: the gate reads the routing and the recorded runs."""
    monkeypatch.setattr(config, "get", lambda: KEYED)


# ---- the gate ------------------------------------------------------------------------------------


def unproven_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if "profile.ai_gate" in r.getMessage()]


def test_a_key_alone_lets_a_real_model_read_resumes_and_the_log_says_nobody_has_measured_it(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    assert reason_of(conn) is None
    (line,) = unproven_lines(caplog)
    assert (line["level"], line["status"], line["model"]) == ("info", "unproven", ROUTE_MODEL)
    assert set(line) - {"ts", "level", "event", "request_id"} == {"status", "model"}  # a code and a model, no text


def test_a_passed_run_that_names_the_model_and_the_prompts_in_use_writes_no_line(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    record_run(conn, proof())
    assert reason_of(conn) is None
    assert unproven_lines(caplog) == []


STALE_PROMPT = {**VERSIONS, workflow.TASKS[0].name: "0123456789ab"}
ONE_TASK_MISSING = {name: version for name, version in list(VERSIONS.items())[1:]}


@pytest.mark.parametrize(
    ("over", "status", "suite"),
    [
        ({"model": "another-model"}, "passed", "resume_extraction"),
        ({"model": None}, "passed", "resume_extraction"),  # a run that names no model
        ({"prompt_versions": STALE_PROMPT}, "passed", "resume_extraction"),  # the prompt has changed since
        ({"prompt_versions": ONE_TASK_MISSING}, "passed", "resume_extraction"),
        ({"prompt_versions": None}, "passed", "resume_extraction"),
        ({"split": "dev"}, "passed", "resume_extraction"),
        ({"method": "rules"}, "passed", "resume_extraction"),
        ({}, "failed", "resume_extraction"),
        ({}, "error", "resume_extraction"),
        ({}, "passed", "harness_smoke"),  # another suite
    ],
    ids=[
        "another model",
        "no model named",
        "an older prompt",
        "a task missing",
        "no prompts named",
        "the dev split",
        "the rules method",
        "a failed run",
        "an errored run",
        "another suite",
    ],
)
def test_a_run_that_differs_in_one_way_is_no_measurement_of_these_routes_but_the_model_still_serves(
    conn, keyed, caplog, over, status, suite
):
    caplog.set_level(logging.INFO, logger="app.event")
    record_run(conn, proof(**over), status=status, suite=suite)
    assert reason_of(conn) is None
    assert len(unproven_lines(caplog)) == 1


def test_the_replay_ci_records_is_no_measurement_of_a_real_model_but_a_live_run_naming_it_is(conn, keyed, caplog):
    """The replay names the model "recorded" and has no method or split (backend/app/evals/runner.py, score.py), so a
    pull request's gate run does not count as a measurement of the real model. A live run records the model it used."""
    caplog.set_level(logging.INFO, logger="app.event")
    replay = {
        "mode": "replay",
        "recorded": "recordings.jsonl",
        "task": workflow.TASKS[0].name,
        "provider": "fake",
        "model": "recorded",
        "slice": "facts",
        "synthetic": True,
        "prompt_versions": VERSIONS,
    }
    record_run(conn, replay)
    assert reason_of(conn) is None and len(unproven_lines(caplog)) == 1
    live = {**replay, "mode": "live", "provider": "anthropic", "method": "model", "split": "test", "model": ROUTE_MODEL}
    record_run(conn, live)
    assert reason_of(conn) is None and len(unproven_lines(caplog)) == 1  # no second line: the live run is on record


def test_the_fake_provider_replays_recordings_so_it_needs_no_run_and_writes_no_line(conn, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(real, llm_provider="fake"))
    assert reason_of(conn) is None  # no run exists
    assert unproven_lines(caplog) == []


def test_no_key_is_still_ai_disabled_and_a_recorded_run_does_not_conjure_a_provider(conn, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(real, llm_provider="anthropic", anthropic_api_key=None))
    record_run(conn, proof())
    assert reason_of(conn) == "no_key"


def test_a_provider_with_no_route_for_the_resume_tasks_is_still_ai_disabled(conn, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(real, llm_provider="nonsense"))
    assert reason_of(conn) == "no_route"  # models.toml names no such provider: nothing is called


def test_openai_has_a_route_for_the_resume_tasks_and_the_check_lets_a_call_through(conn, monkeypatch):
    real = config.get()
    keyed_openai = replace(real, llm_provider="openai", openai_api_key="k", anthropic_api_key=None, llm_base_url=None)
    monkeypatch.setattr(config, "get", lambda: keyed_openai)
    assert reason_of(conn) is None  # the consent, not a missing route, is what keeps a resume from OpenAI until agreed


def test_three_tasks_on_different_models_serve_and_the_log_names_them_all(conn, keyed, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    real_route = registry.route
    other = {workflow.TASKS[1].name: "a-different-model"}
    monkeypatch.setattr(
        registry,
        "route",
        lambda task, settings: replace(real_route(task, settings), model=other.get(task, ROUTE_MODEL)),
    )
    record_run(conn, proof())  # one run names one model, so it cannot vouch for three tasks on two
    assert reason_of(conn) is None
    (line,) = unproven_lines(caplog)
    assert line["model"] == f"a-different-model, {ROUTE_MODEL}"


# ---- the upload ----------------------------------------------------------------------------------


def upload(client, data, name="resume.docx"):
    return client.post(
        URL, files={"file": (name, data, "application/octet-stream")}, headers={"Idempotency-Key": uuid4().hex}
    )


def error_of(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def test_an_upload_on_a_real_model_nobody_has_measured_goes_on_to_ask_for_the_consent(signed_in, kit):
    """The upload reads eval_runs as the API role, so the grant that role has on it is exercised here."""
    kit.provider("anthropic", key="k")
    error_of(upload(signed_in, kit.docx()), 403, "consent_required")  # not "AI is off": a key is enough to get this far
    kit.agree(signed_in.user)
    assert upload(signed_in, kit.docx()).status_code == 202


def test_an_upload_with_no_provider_still_says_ai_is_off_before_it_asks_for_a_consent(signed_in, kit):
    kit.provider("anthropic", key=None)
    for agreed in (False, True):
        if agreed:
            kit.agree(signed_in.user)
        error = error_of(upload(signed_in, kit.docx()), 503, "ai_disabled")
        assert "needs an AI model" in error["message"]
    assert (kit.count("documents"), kit.count("queue_jobs"), kit.count("llm_calls")) == (0, 0, 0)
    assert signed_in.get("/v1/me/record").status_code == 200  # the record still works


# ---- the parse job -------------------------------------------------------------------------------


def test_a_queued_parse_fails_as_ai_disabled_when_there_is_no_provider_and_the_model_is_never_called(kit):
    kit.provider("anthropic", key=None)
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    model = kit.model()

    assert kit.run(model) == 1

    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "ai_disabled")
    assert model.calls == [] and kit.facts(document_id) == [] and kit.count("llm_calls") == 0
    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)  # finished, not retried


def test_a_queued_parse_runs_on_a_real_model_nobody_has_measured(kit):
    kit.provider("anthropic", key="k")
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    model = kit.model()

    assert kit.run(model) == 1  # the worker role reads the runs too, to know whether to write the log line

    assert kit.document(document_id).state == "parsed" and len(model.calls) == 3
