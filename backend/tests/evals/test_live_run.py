"""The live evidence_faithfulness run: `python -m app.evals run evidence_faithfulness --live` records a model-method run
that names the model and the prompt, which is what a published number for that model reads (app.match.ai_gate decides
only whether to log that nobody has measured the route). It is a measurement: no run opens or closes the route.

A live run calls the real model the environment names, so these tests stand in for it: the anthropic route of
models.toml with a key, and the n-th answer to the n-th request taken from the gold recording (or from the degraded
one). Nothing reaches a provider. The runner and the gateway commit on connections of their own, so the tests use the
eval tests' private database (the `engine` fixture).
"""

import json
import os
from dataclasses import replace

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.evals.__main__ import choose_provider, main
from app.match import ai_gate, evidence
from app.match.validator import VerdictSet
from app.platform import config
from app.platform.llm import AIDisabled, ProviderUnavailable, registry

SUITE = "evidence_faithfulness"
SUITE_DIR = EVALS / SUITE
GOLD_ANSWERS = json.loads((SUITE_DIR / "recorded.json").read_text(encoding="utf-8"))
DEGRADED_ANSWERS = json.loads((SUITE_DIR / "fixtures" / "degraded.json").read_text(encoding="utf-8"))
MARKER = "agent-drafted, maintainer review pending"


def real_model(monkeypatch, answers):
    """The anthropic route with a key, and a stand-in for the model that answers the n-th request with the n-th entry of
    `answers`. Returns the list of who each request was charged to."""
    settings = replace(
        config.get(), llm_provider="anthropic", anthropic_api_key="k", openai_api_key=None, llm_base_url=None
    )
    monkeypatch.setattr(config, "get", lambda: settings)
    remaining, charged = iter(answers), []

    def ask(request, *, principal):
        charged.append(principal)
        return VerdictSet.model_validate(next(remaining))

    monkeypatch.setattr(evidence, "score", ask)
    return charged


def runs(engine):
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT suite::text AS suite, status::text AS status, config, metrics FROM eval_runs"))
            .mappings()
            .all()
        )


def route_serves(engine) -> bool:
    """Whether the match run may call the real model now: the route check's own answer, on the same database."""
    with engine.connect() as conn:
        try:
            ai_gate.require_available(conn)
        except AIDisabled:
            return False
    return True


def is_measured(engine) -> bool:
    """Whether a passed run names this route's model and prompt: what the route check logs "unproven" for."""
    route = registry.route(evidence.TASK, config.get())
    found = {"model": route.model, "prompt_version": evidence.PROMPT_VERSION}
    with engine.connect() as conn:
        return bool(conn.execute(ai_gate.ROUTE_IS_PROVEN, found).scalar_one())


# ---- what a live run records ---------------------------------------------------------------------------


def test_a_live_run_that_meets_the_bars_is_a_model_method_run_that_names_the_model_and_the_prompt(engine, monkeypatch):
    charged = real_model(monkeypatch, GOLD_ANSWERS)
    route = registry.route(evidence.TASK, config.get())
    assert route_serves(engine) is True and is_measured(engine) is False  # a key alone serves; nobody has measured it

    done = runner.run_suite(SUITE, live=True)

    assert (done.status, done.passed, done.n) == ("passed", 30, 30)
    (run,) = runs(engine)
    assert run["suite"] == SUITE and run["status"] == "passed"
    assert run["config"] == {
        "mode": "live",
        "method": "model",
        "task": "match.evidence.score",
        "provider": "anthropic",
        "model": route.model,
        "labels": MARKER,
        "prompt_version": evidence.PROMPT_VERSION,
        "gold": "gold.jsonl",
    }
    assert (
        run["metrics"]["score"] == 1.0 and run["metrics"]["harness_check_only"] is False
    )  # a measurement, not a replay
    assert len(charged) == 30 and set(charged) == {None}  # one request per pair, charged to nobody: an evaluation run
    assert route_serves(engine) is True and is_measured(engine) is True  # served before, and now measured
    assert gate.main(SUITE, done.run_id) == 0


def test_a_live_run_that_misses_a_bar_is_recorded_failed_and_the_route_still_serves(engine, monkeypatch):
    real_model(monkeypatch, DEGRADED_ANSWERS)  # five false mets on required items and two sets that cite nothing real

    done = runner.run_suite(SUITE, live=True)

    assert done.status == "failed" and done.passed < done.n
    (run,) = runs(engine)
    assert (run["status"], run["config"]["method"], run["metrics"]["score"]) == ("failed", "model", 0.0)
    assert route_serves(engine) is True  # a failed run is a number on record for good: it closes nothing
    assert is_measured(engine) is False
    assert gate.main(SUITE, done.run_id) == 1


def test_a_provider_that_is_down_mid_run_ends_the_run_in_error_and_measures_nothing(engine, monkeypatch):
    real_model(monkeypatch, GOLD_ANSWERS)

    def down(request, *, principal):
        raise ProviderUnavailable()

    monkeypatch.setattr(evidence, "score", down)

    done = runner.run_suite(SUITE, live=True)

    assert done.status == "error" and route_serves(engine) is True and is_measured(engine) is False
    assert gate.main(SUITE, done.run_id) == 1


def test_the_replay_the_ci_records_still_has_no_method_and_is_no_measurement_of_a_real_model(engine, monkeypatch):
    runner.run_suite(SUITE)  # the fake provider the fixture chose: a replay of the recording
    (run,) = runs(engine)
    assert run["config"]["mode"] == "replay" and "method" not in run["config"]
    assert run["metrics"]["harness_check_only"] is True
    real_model(monkeypatch, GOLD_ANSWERS)  # now a real model is configured, and the replay is all there is on record
    assert is_measured(engine) is False


# ---- what a live run refuses, before anything is written -------------------------------------------------------------


def test_a_live_run_with_the_fake_provider_replays_nothing_and_writes_nothing(engine):
    with pytest.raises(runner.LiveNeedsRealProvider):
        runner.run_suite(SUITE, live=True)
    assert runs(engine) == []


def test_a_live_run_with_no_key_is_refused_by_the_registry_and_writes_nothing(engine, monkeypatch):
    settings = replace(config.get(), llm_provider="anthropic", anthropic_api_key=None)
    monkeypatch.setattr(config, "get", lambda: settings)
    with pytest.raises(AIDisabled) as refused:
        runner.run_suite(SUITE, live=True)
    assert refused.value.reason == "no_key" and runs(engine) == []


@pytest.mark.parametrize("suite", ["harness_smoke", "constraint_property", "tailoring_integrity", "chat", "guardian"])
def test_a_suite_with_no_live_mode_refuses_one(engine, monkeypatch, suite):
    """Only a suite whose scorer declares LIVE has one: the refusal comes before any model is asked. (The three suites
    that do have one are tested, behind a stand-in for the provider, in test_live_suites.py.)"""
    real_model(monkeypatch, [])
    with pytest.raises(runner.LiveNotSupported):
        runner.run_suite(suite, live=True)
    assert runs(engine) == []


def test_a_live_run_has_no_recording_to_replay(engine, monkeypatch):
    real_model(monkeypatch, GOLD_ANSWERS)
    with pytest.raises(ValueError):
        runner.run_suite(SUITE, SUITE_DIR / "recorded.json", live=True)
    assert runs(engine) == []


# ---- the command ---------------------------------------------------------------------------------------


def test_the_command_runs_live_prints_the_run_id_last_and_the_gate_takes_it(engine, monkeypatch, capsys):
    real_model(monkeypatch, GOLD_ANSWERS)

    assert main(["run", SUITE, "--live"]) == 0

    out = capsys.readouterr()
    run_id = out.out.strip().splitlines()[-1]
    assert f"{SUITE}: 30 of 30 items passed, status passed" in out.err
    assert main(["gate", SUITE, "--run", run_id]) == 0 and is_measured(engine) is True


def test_the_command_says_what_is_missing_and_exits_2(engine, monkeypatch, capsys):
    assert main(["run", SUITE, "--live"]) == 2  # the fixture's provider is the fake one
    assert "--live calls a real model" in capsys.readouterr().err
    assert main(["run", "harness_smoke", "--live"]) == 2
    assert "no live mode" in capsys.readouterr().err
    settings = replace(config.get(), llm_provider="anthropic", anthropic_api_key=None)
    monkeypatch.setattr(config, "get", lambda: settings)
    assert main(["run", SUITE, "--live"]) == 2
    assert "no_key" in capsys.readouterr().err
    assert runs(engine) == []


def test_the_command_refuses_a_recording_with_live(capsys):
    with pytest.raises(SystemExit) as refused:
        main(["run", SUITE, "--live", "--recorded", str(SUITE_DIR / "recorded.json")])
    assert refused.value.code == 2 and "no recording to replay" in capsys.readouterr().err


def test_only_a_live_run_keeps_the_provider_the_environment_chose():
    """A replay needs no model, so it runs on the fake provider whatever the environment says; --live is the one run
    that calls a real model."""
    before = os.environ.get("LLM_PROVIDER")
    replay, live = {"LLM_PROVIDER": "anthropic"}, {"LLM_PROVIDER": "anthropic"}
    choose_provider(["run", SUITE], replay)
    choose_provider(["run", SUITE, "--live"], live)
    assert replay["LLM_PROVIDER"] == "fake" and live["LLM_PROVIDER"] == "anthropic"
    assert os.environ.get("LLM_PROVIDER") == before  # the function changes the mapping it is given and nothing else


# ---- the documentation of the command ------------------------------------------------------------------


def test_the_suite_readme_and_the_ai_system_doc_give_the_command_and_say_it_opens_nothing():
    command = "python -m app.evals run evidence_faithfulness --live"
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    assert command in readme and "DATABASE_URL_WORKER" in readme and "ANTHROPIC_API_KEY" in readme
    assert "`method`" in readme and "`model`" in readme and "`prompt_version`" in readme
    assert "opens no feature" in readme
    ai_system = (EVALS.parent / "docs" / "AI_SYSTEM.md").read_text(encoding="utf-8")
    assert command in ai_system and "match/ai_gate.py" in ai_system
