"""The model gateway: validated output, one ledger row per provider request, nothing stored but numbers (PLT-04)."""

import logging
import traceback

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.platform import db
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, complete, fake
from app.platform.llm.__main__ import PingResult
from app.platform.llm.adapters import Reply
from app.platform.llm.gateway import cost_microusd, usd

MESSAGES = [{"role": "user", "content": "say pong"}]
PONG = {"reply": "pong"}


def ask(principal=None):
    return complete("ping", PingResult, MESSAGES, principal=principal, prompt_version="v1")


def test_a_valid_answer_is_parsed_and_costed_in_one_ledger_row(harness):
    harness.use_priced_fake()  # 100 input and 20 output tokens at 2.5 and 10 dollars per million tokens
    harness.record(PONG)
    user = harness.user()
    assert ask(user) == PingResult(reply="pong")
    (row,) = harness.ledger()
    assert row["latency_ms"] >= 0
    assert {k: v for k, v in row.items() if k != "latency_ms"} == {
        "task": "ping", "prompt_version": "v1", "outcome": "ok", "user_id": user, "input_tokens": 100,
        "output_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_microusd": 450, "attempt": 1,
        "provider": "fake", "model_name": "recorded",
    }  # fmt: skip


def test_invalid_output_then_valid_output_succeeds_after_one_retry(harness):
    harness.record("not json at all", PONG)
    assert ask() == PingResult(reply="pong")
    assert [(r["outcome"], r["attempt"]) for r in harness.ledger()] == [("schema_invalid", 1), ("ok", 2)]


@pytest.mark.parametrize("answer", ["not json", {"nope": 1}])
def test_output_invalid_twice_is_rejected_and_recorded_both_times(harness, answer):
    harness.record(answer, answer)
    with pytest.raises(SchemaInvalid) as rejected:
        ask()
    assert str(rejected.value) == "schema_invalid" and not rejected.value.refused
    assert [r["outcome"] for r in harness.ledger()] == ["schema_invalid", "schema_invalid"]


def test_a_refusal_is_retried_once_then_recorded_as_refused(harness, monkeypatch):
    monkeypatch.setattr(fake, "call", lambda task: Reply("", 100, 20, refused=True))
    with pytest.raises(SchemaInvalid) as rejected:
        ask()
    assert rejected.value.refused
    assert [r["outcome"] for r in harness.ledger()] == ["refused", "refused"]


@pytest.mark.parametrize(("timed_out", "outcome"), [(True, "timeout"), (False, "provider_error")])
def test_a_provider_failure_is_recorded_once_and_not_retried(harness, monkeypatch, timed_out, outcome):
    def fails(task):
        raise ProviderUnavailable(timed_out=timed_out)

    monkeypatch.setattr(fake, "call", fails)
    with pytest.raises(ProviderUnavailable) as failed:
        ask()
    assert failed.value.status == 503 and failed.value.code == "dependency_unavailable"
    (row,) = harness.ledger()
    assert (row["outcome"], row["input_tokens"], row["cost_microusd"]) == (outcome, 0, 0)


def test_nothing_anyone_wrote_reaches_the_ledger_the_log_or_an_error(harness, caplog):
    prompt, answer = "CANARY-PROMPT-7f3a", "CANARY-ANSWER-91bc"
    harness.use_priced_fake()
    caplog.set_level(logging.DEBUG)
    harness.record(f"not json {answer}", {"reply": answer}, f"bad {answer}", f"bad {answer}")
    result = complete("ping", PingResult, [{"role": "user", "content": prompt}], principal=None, prompt_version="v1")
    assert result.reply == answer  # the caller gets the text and nobody else does
    with pytest.raises(SchemaInvalid) as rejected:
        complete("ping", PingResult, [{"role": "user", "content": prompt}], principal=None, prompt_version="v1")
    shown = "".join(traceback.format_exception(rejected.value)) + repr(rejected.value.__context__)
    for place in (harness.everything_stored(), caplog.text, shown):
        assert prompt not in place and answer not in place
    assert any("llm.call" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_PROVIDER": "anthropic"},  # the default, with no key
        {"LLM_PROVIDER": "openai_compatible"},  # OpenAI, with no key
        {"LLM_PROVIDER": "nonsense"},
        {"LLM_PROVIDER": "fake", "APP_ENV": "production"},  # T-01-19
    ],
)
def test_no_usable_provider_raises_ai_disabled_and_writes_nothing(harness, env):
    harness.configure(**env)
    with pytest.raises(AIDisabled):
        ask()
    assert harness.ledger() == []


def test_the_api_answers_ai_disabled_as_503(harness):
    harness.configure(LLM_PROVIDER="anthropic")
    app = create_app()
    app.add_api_route("/needs-ai", lambda: ask())
    body = TestClient(app).get("/needs-ai")
    assert body.status_code == 503 and body.json()["error"]["code"] == "ai_disabled"


def test_a_caller_that_rolls_back_still_leaves_the_ledger_row(harness):
    harness.record(PONG)
    with pytest.raises(RuntimeError), db.transaction(role="worker"):
        ask()
        raise RuntimeError("the caller fails after the model answered")
    assert len(harness.ledger()) == 1


@pytest.mark.parametrize(
    ("tokens", "prices", "expected"),
    [((100, 20), (2_500_000, 10_000_000), 450), ((5, 0), (100_000, 500_000), 1), ((4, 0), (100_000, 500_000), 0),
     ((1_000_000, 1_000_000), (100_000, 500_000), 600_000), ((100, 20), (None, None), 0)],
)  # fmt: skip
def test_cost_is_whole_micro_dollars_rounded_half_up_and_zero_without_a_price(tokens, prices, expected):
    assert cost_microusd(*tokens, *prices) == expected


def test_money_is_formatted_from_integers():
    assert [usd(n) for n in (0, 1, 1_234_567, 25_000_000)] == ["0.000000", "0.000001", "1.234567", "25.000000"]
