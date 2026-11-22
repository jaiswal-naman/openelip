"""Routing is configuration (PLT-05): the same call goes to another provider with no code change."""

import pytest
from sqlalchemy import text

from app.platform.llm import AIDisabled, adapters, complete, fake, registry
from app.platform.llm.__main__ import PingResult, main
from app.platform.llm.adapters import Reply


@pytest.fixture
def calls(monkeypatch):
    """Both real adapters replaced by recorders, so no SDK and no network is touched."""
    seen = []

    def recorder(name):
        def adapter(route, messages, schema):
            seen.append((name, route))
            return Reply('{"reply": "pong"}', 100, 20)

        return adapter

    monkeypatch.setattr(adapters, "call_anthropic", recorder("anthropic"))
    monkeypatch.setattr(adapters, "call_openai_compatible", recorder("openai_compatible"))
    return seen


def ask():
    return complete("ping", PingResult, [{"role": "user", "content": "x"}], principal=None, prompt_version="v1")


@pytest.mark.parametrize(
    ("env", "adapter", "model", "base_url", "cost"),
    [
        ({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}, "anthropic", "claude-haiku-5-5", None, 20),
        ({"LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "k"}, "openai_compatible", "gpt-6-luna",
         "https://api.openai.com/v1", 20),
        ({"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://llm:11434/v1"}, "openai_compatible",
         "local-model", "http://llm:11434/v1", 0),
    ],
)  # fmt: skip
def test_the_task_moves_across_three_providers_by_environment_alone(
    harness, calls, env, adapter, model, base_url, cost
):
    harness.configure(**env)
    ask()
    ((name, route),) = calls
    assert (name, route.model, route.base_url) == (adapter, model, base_url)
    assert (
        harness.ledger()[0]["cost_microusd"] == cost
    )  # 100 and 20 tokens at the shipped prices; a local model is free
    if base_url == "http://llm:11434/v1":  # the local server only has JSON mode and the older token limit
        assert (route.structured_output, route.token_limit_param, route.api_key) == (
            "json_object",
            "max_tokens",
            "not-needed",
        )


def test_an_edit_to_the_toml_changes_the_model_and_the_price_with_no_code_change(harness, calls):
    harness.configure(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
    harness.toml(
        """
[providers.anthropic]
label = "A"
adapter = "anthropic"
retention_note = "n"
[tasks.ping]
description = "d"
data_class = "synthetic"
max_tokens = 8
timeout_seconds = 1
[tasks.ping.models.anthropic]
model = "claude-sonnet-5-5"
input_usd_per_mtok = 2.0
output_usd_per_mtok = 10.0
price_checked_at = 2026-11-22
"""
    )
    ask()
    assert calls[0][1].model == "claude-sonnet-5-5"
    assert harness.ledger()[0]["cost_microusd"] == 400  # (100 x 2.0 + 20 x 10.0) per million tokens, in micro-dollars


def test_a_price_nobody_checked_is_refused_never_guessed(harness, calls):
    harness.configure(LLM_PROVIDER="openai_compatible", OPENAI_API_KEY="k")
    harness.toml(
        """
[providers.openai]
label = "O"
adapter = "openai_compatible"
base_url = "https://api.openai.com/v1"
retention_note = "n"
[tasks.ping]
description = "d"
data_class = "synthetic"
max_tokens = 8
timeout_seconds = 1
[tasks.ping.models.openai]
model = "m"
input_usd_per_mtok = "unknown"
output_usd_per_mtok = 0.5
price_checked_at = 2026-11-22
"""
    )
    with pytest.raises(AIDisabled):
        ask()
    assert calls == [] and harness.ledger() == []


def test_the_fake_serves_any_task_that_has_a_recording(harness):
    harness.record({"reply": "pong"})
    assert complete("some.other.task", PingResult, [], principal=None, prompt_version="v1").reply == "pong"
    assert harness.ledger()[0]["task"] == "some.other.task"


def test_the_registry_row_is_created_on_first_use_without_a_sync(harness):
    harness.record({"reply": "pong"})
    ask()
    with harness.engine.connect() as conn:
        assert conn.execute(text("SELECT provider, model_name, kind::text FROM model_registry")).all() == [
            ("fake", "recorded", "chat")
        ]


def test_sync_mirrors_the_file_one_row_per_model_and_retires_the_rest(harness):
    with harness.engine.begin() as conn:
        assert registry.sync(conn) == 4  # anthropic, openai, local, fake
        conn.execute(
            text(
                "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
                "VALUES ('old', 'm', 'chat', true, 'x')"
            )
        )
        assert registry.sync(conn) == 4
        found = conn.execute(
            text(
                "SELECT provider, model_name, is_active, input_microusd_per_mtok, output_microusd_per_mtok "
                "FROM model_registry"
            )
        )
        rows = {(p, m): (a, i, o) for p, m, a, i, o in found}
    assert rows[("anthropic", "claude-haiku-5-5")] == (True, 100_000, 500_000)
    assert rows[("openai", "gpt-6-luna")] == (True, 100_000, 500_000)
    assert rows[("local", "local-model")] == (True, None, None)
    assert rows[("old", "m")][0] is False and len(rows) == 5


def test_ping_writes_a_row_for_the_user_and_prints_its_id_and_cost(harness, capsys):
    assert main(["ping", "--email", "x@example.test"]) == 0  # shipped config: the fake costs nothing
    (row,) = harness.ledger()
    assert row["user_id"] == harness.user("x@example.test") and row["task"] == "ping" and row["cost_microusd"] == 0
    assert "cost $0.000000" in capsys.readouterr().out
    harness.use_priced_fake()  # test prices, to see a priced row
    fake.replay_from(None)
    assert main(["ping", "--email", "x@example.test"]) == 0
    assert harness.ledger()[1]["cost_microusd"] == 450 and "cost $0.000450" in capsys.readouterr().out


def test_ping_with_no_key_says_so_and_writes_nothing(harness, capsys):
    harness.configure(LLM_PROVIDER="anthropic")
    assert main(["ping", "--email", "x@example.test"]) == 1
    assert "AI is disabled" in capsys.readouterr().err and harness.ledger() == []
