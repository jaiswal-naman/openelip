"""A real model scores evidence only after a passed gold-set run proves its route (AI_SYSTEM 2.2 and 12.8; W1).

models.toml names claude-haiku-5-5 for match.evidence.score, and no passed evidence_faithfulness run by the model method
exists, so a key alone must not start sending a candidate's facts to a model nobody has measured, nor show matches from
its verdicts. The run then completes without scoring: every retrieved job is withheld not_scored, as with no key. The
fake provider replays recordings and calls no model, so it needs no proof. Nothing here calls a model: the gate only
reads the routing and the recorded runs.
"""

import json
import logging
from dataclasses import replace

import pytest
from sqlalchemy import text

from app.match import ai_gate, evidence
from app.platform import config
from app.platform.llm import AIDisabled, registry

KEYED = replace(config.get(), llm_provider="anthropic", anthropic_api_key="k", openai_api_key=None, llm_base_url=None)
ROUTE_MODEL = registry.route(evidence.TASK, KEYED).model  # what models.toml says answers the task


def proof(**over):
    """The config of a run that proves the route: the model method, naming the model the route uses and the prompt in
    use. (The suite has no split yet: its README chooses one when the first live run is planned.)"""
    return {"method": "model", "model": ROUTE_MODEL, "prompt_version": evidence.PROMPT_VERSION, **over}


def record_run(conn, run_config, *, suite="evidence_faithfulness", status="passed"):
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


def test_a_key_alone_does_not_let_a_real_model_score_evidence(conn, keyed):
    assert reason_of(conn) == "unproven"


def test_a_closed_gate_says_why_in_the_log_with_the_code_and_the_model_and_nothing_else(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    assert reason_of(conn) == "unproven"
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if "match.ai_gate" in r.getMessage()]
    assert (line["level"], line["error_code"], line["model"]) == ("warning", "unproven", ROUTE_MODEL)
    assert set(line) - {"ts", "level", "event", "request_id"} == {"error_code", "model"}


def test_a_passed_run_that_names_the_model_and_the_prompt_in_use_opens_the_gate(conn, keyed):
    record_run(conn, proof())
    assert reason_of(conn) is None


@pytest.mark.parametrize(
    ("over", "status", "suite"),
    [
        ({"model": "another-model"}, "passed", "evidence_faithfulness"),
        ({"model": None}, "passed", "evidence_faithfulness"),  # a run that names no model
        ({"prompt_version": "0123456789ab"}, "passed", "evidence_faithfulness"),  # the prompt has changed since
        ({"prompt_version": None}, "passed", "evidence_faithfulness"),
        ({"method": "rules"}, "passed", "evidence_faithfulness"),
        ({"method": None}, "passed", "evidence_faithfulness"),
        ({}, "failed", "evidence_faithfulness"),
        ({}, "error", "evidence_faithfulness"),
        ({}, "passed", "resume_extraction"),  # another suite
    ],
    ids=[
        "another model",
        "no model named",
        "an older prompt",
        "no prompt named",
        "the rules method",
        "no method",
        "a failed run",
        "an errored run",
        "another suite",
    ],
)
def test_a_run_that_differs_in_one_way_does_not_prove_the_route(conn, keyed, over, status, suite):
    record_run(conn, proof(**over), status=status, suite=suite)
    assert reason_of(conn) == "unproven"


def test_the_replay_the_ci_records_never_proves_a_real_model_but_a_live_run_naming_it_would(conn, keyed):
    """The replay names the model "recorded" and has no method (evals/evidence_faithfulness/score.py), so a pull
    request's gate run cannot open the gate. A live run adds the method and the model it used."""
    replay = {
        "mode": "replay",
        "recorded": "recorded.json",
        "task": evidence.TASK,
        "provider": "fake",
        "model": "recorded",
        "labels": "agent-drafted, maintainer review pending",
        "prompt_version": evidence.PROMPT_VERSION,
        "gold": "gold.jsonl",
    }
    record_run(conn, replay)
    assert reason_of(conn) == "unproven"
    record_run(conn, {**replay, "mode": "live", "provider": "anthropic", "method": "model", "model": ROUTE_MODEL})
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
