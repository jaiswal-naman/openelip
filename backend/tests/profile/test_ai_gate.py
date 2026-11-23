"""A real model reads a resume only after a passed gold-set run proves its route (AI_SYSTEM 2.2; verification W2).

models.toml names claude-haiku-5-5 (and a local placeholder) for the three resume.facts tasks, and no passed
resume_extraction run by the model method exists, so a key alone must not start sending resumes. The upload answers 503
ai_disabled (the AI-off a person sees with no key) and a queued parse fails as ai_disabled; the fake provider, which
replays recordings and calls no model, needs no proof. Nothing here calls a model: the gate only reads routing and runs.
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


def test_a_key_alone_does_not_let_a_real_model_read_resumes(conn, keyed):
    assert reason_of(conn) == "unproven"


def test_a_closed_gate_says_why_in_the_log_with_the_code_and_the_model_and_nothing_else(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    assert reason_of(conn) == "unproven"
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if "profile.ai_gate" in r.getMessage()]
    assert (line["level"], line["error_code"], line["model"]) == ("warning", "unproven", ROUTE_MODEL)
    assert set(line) - {"ts", "level", "event", "request_id"} == {"error_code", "model"}


def test_a_passed_run_that_names_the_model_and_the_prompts_in_use_opens_the_gate(conn, keyed):
    record_run(conn, proof())
    assert reason_of(conn) is None


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
def test_a_run_that_differs_in_one_way_does_not_prove_the_routes(conn, keyed, over, status, suite):
    record_run(conn, proof(**over), status=status, suite=suite)
    assert reason_of(conn) == "unproven"


def test_the_replay_ci_records_never_proves_a_real_model_but_a_live_run_naming_it_would(conn, keyed):
    """The replay names the model "recorded" and has no method or split (backend/app/evals/runner.py, score.py), so a
    pull request's gate run cannot open the gate. What a live run records, besides that, is what opens it."""
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
    assert reason_of(conn) == "unproven"
    live = {**replay, "mode": "live", "provider": "anthropic", "method": "model", "split": "test", "model": ROUTE_MODEL}
    record_run(conn, live)
    assert reason_of(conn) is None


def test_the_fake_provider_replays_recordings_so_it_needs_no_proof(conn, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(real, llm_provider="fake"))
    assert reason_of(conn) is None  # no run exists


def test_no_provider_is_ai_disabled_for_its_own_reason_not_unproven(conn, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(real, llm_provider="anthropic", anthropic_api_key=None))
    record_run(conn, proof())  # a proof does not conjure a provider
    assert reason_of(conn) == "no_key"


def test_three_tasks_on_different_models_cannot_be_proven_by_one_run(conn, keyed, monkeypatch):
    real_route = registry.route
    other = {workflow.TASKS[1].name: "a-different-model"}
    monkeypatch.setattr(
        registry,
        "route",
        lambda task, settings: replace(real_route(task, settings), model=other.get(task, ROUTE_MODEL)),
    )
    record_run(conn, proof())
    assert reason_of(conn) == "unproven"


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


def test_an_upload_on_a_real_model_nobody_has_measured_says_ai_is_off_before_it_asks_for_a_consent(signed_in, kit):
    kit.provider("anthropic", key="k")
    for agreed in (False, True):  # an agreement changes nothing while no passed run proves the route
        if agreed:
            kit.agree(signed_in.user)
        error = error_of(upload(signed_in, kit.docx()), 503, "ai_disabled")
        assert "needs an AI model" in error["message"]
    assert (kit.count("documents"), kit.count("queue_jobs"), kit.count("llm_calls")) == (0, 0, 0)
    assert signed_in.get("/v1/me/record").status_code == 200  # the record still works


def test_a_proven_real_model_lets_the_upload_through_once_the_person_has_agreed(signed_in, kit):
    """The upload reads eval_runs as the API role, so the grant that role has on it is exercised here."""
    kit.provider("anthropic", key="k")
    kit.prove()
    error_of(upload(signed_in, kit.docx()), 403, "consent_required")  # proven, but not agreed
    kit.agree(signed_in.user)
    assert upload(signed_in, kit.docx()).status_code == 202


# ---- the parse job -------------------------------------------------------------------------------


def test_a_queued_parse_fails_as_ai_disabled_when_the_route_is_not_proven_and_the_model_is_never_called(kit):
    """A person uploaded while the route was proven, or before the prompts changed. The job checks again."""
    kit.provider("anthropic", key="k")
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    model = kit.model()

    assert kit.run(model) == 1

    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "ai_disabled")
    assert model.calls == [] and kit.facts(document_id) == [] and kit.count("llm_calls") == 0
    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)  # finished, not retried


def test_a_queued_parse_runs_when_the_route_is_proven(kit):
    kit.provider("anthropic", key="k")
    kit.prove()
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    model = kit.model()

    assert kit.run(model) == 1  # the worker role reads the proof too

    assert kit.document(document_id).state == "parsed" and len(model.calls) == 3
