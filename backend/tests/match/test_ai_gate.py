"""A real model scores evidence whether or not a gold-set run has measured it (AI_SYSTEM 2.2 and 12.8).

models.toml names claude-haiku-5-5 for match.evidence.score. The gold set measures accuracy, it does not decide
whether a model may be used, so a key alone is enough: the match run scores the candidate's facts with it. What still
means "AI is off" is a route that cannot be called (no route, no key, an unpriced model, the fake in production): the
run completes without scoring and every retrieved job is withheld not_scored. A real model with no passed
evidence_faithfulness run by the model method, naming the model and the prompt in use, is written to the log as one
info line (status "unproven"); a measured one writes nothing. Nothing here calls a model: the check only reads routing
and runs.
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
    """Why the check refuses the route, or None when it lets a call through."""
    try:
        ai_gate.require_available(conn)
    except AIDisabled as closed:
        return closed.reason
    return None


@pytest.fixture
def keyed(monkeypatch):
    """A real model with a key. Nothing is called: the check reads the routing and the recorded runs."""
    monkeypatch.setattr(config, "get", lambda: KEYED)


def unproven_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if "match.ai_gate" in r.getMessage()]


def test_a_key_alone_lets_a_real_model_score_evidence_and_the_log_says_nobody_has_measured_it(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    assert reason_of(conn) is None
    (line,) = unproven_lines(caplog)
    assert (line["level"], line["status"], line["model"]) == ("info", "unproven", ROUTE_MODEL)
    assert set(line) - {"ts", "level", "event", "request_id"} == {"status", "model"}  # a code and a model, no text


def test_a_passed_run_that_names_the_model_and_the_prompt_in_use_writes_no_line(conn, keyed, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    record_run(conn, proof())
    assert reason_of(conn) is None
    assert unproven_lines(caplog) == []


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
def test_a_run_that_differs_in_one_way_is_no_measurement_of_this_route_but_the_model_still_serves(
    conn, keyed, caplog, over, status, suite
):
    caplog.set_level(logging.INFO, logger="app.event")
    record_run(conn, proof(**over), status=status, suite=suite)
    assert reason_of(conn) is None
    assert len(unproven_lines(caplog)) == 1


def test_the_replay_the_ci_records_is_no_measurement_of_a_real_model_but_a_live_run_naming_it_is(conn, keyed, caplog):
    """The replay names the model "recorded" and has no method (evals/evidence_faithfulness/score.py), so a pull
    request's gate run does not count as a measurement of the real model. A live run adds the method and the model it
    used."""
    caplog.set_level(logging.INFO, logger="app.event")
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
    assert reason_of(conn) is None and len(unproven_lines(caplog)) == 1
    record_run(conn, {**replay, "mode": "live", "provider": "anthropic", "method": "model", "model": ROUTE_MODEL})
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
