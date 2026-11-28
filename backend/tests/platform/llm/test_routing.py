"""Routing is configuration (PLT-05): the same call goes to another provider with no code change."""

import pytest
from sqlalchemy import text

from app.platform import config
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


# OpenAI's route for each task: the model of the same price as the Anthropic route (luna for haiku, sol for sonnet), and
# the reasoning effort the docs allow for it. gpt-6-luna's page lists low; twin.classify is a classification with a
# 300-token cap, which the reasoning guide names as a use of none. gpt-6.2-sol's page lists no values, so none is sent.
LUNA, SOL = "gpt-6-luna", "gpt-6.2-sol"
OPENAI_ROUTES = {
    "ping": (LUNA, "low"),
    "job.requirements.extract": (LUNA, "low"),
    "resume.facts.roles": (LUNA, "low"),
    "resume.facts.education_skills": (LUNA, "low"),
    "resume.facts.summary": (LUNA, "low"),
    "match.evidence.score": (LUNA, "low"),
    "record.propose": (LUNA, "low"),
    "verify.entail": (LUNA, "low"),
    "research.read_page": (LUNA, "low"),
    "twin.classify": (LUNA, "none"),
    "chat.turn": (SOL, None),
    "tailor.propose": (SOL, None),
    "writer.packet": (SOL, None),
    "research.plan": (SOL, None),
    "chaser.draft": (SOL, None),
    "coach.prep": (SOL, None),
    "twin.answer": (SOL, None),
}
OPENAI_PRICES = {LUNA: (100_000, 500_000), SOL: (2_000_000, 10_000_000)}


def test_openai_serves_every_task_with_the_model_of_the_same_price_and_the_documented_effort():
    found = [r for r in registry.table() if r.provider == "openai"]
    assert {r.task: (r.model, r.reasoning_effort) for r in found} == OPENAI_ROUTES
    assert {r.task for r in registry.table()} == set(OPENAI_ROUTES)  # no task is left without an OpenAI route
    for r in found:
        assert (r.input_microusd_per_mtok, r.output_microusd_per_mtok) == OPENAI_PRICES[r.model]
        assert r.price_checked_at is not None and r.price_checked_at.date().isoformat() == "2026-11-28" and r.priced
    note = found[0].retention_note
    for said in ("2026-11-28", "not used for training", "up to 30 days", "may contain prompts and responses"):
        assert said in note
    assert "zero data retention needs OpenAI's approval" in note and "never identified" not in note


def test_every_task_resolves_on_openai_with_a_key_and_the_identified_ones_too():
    keyed = config.load({"APP_ENV": "test", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"})
    for task, (model, effort) in OPENAI_ROUTES.items():
        chosen = registry.route(task, keyed)
        assert (chosen.provider, chosen.model, chosen.reasoning_effort) == ("openai", model, effort)
    assert registry.route("chat.turn", keyed).data_class == "identified"


@pytest.mark.parametrize(
    ("adapter", "effort"),
    [("openai_compatible", "turbo"), ("anthropic", "low")],
    ids=["unknown_value", "wrong_adapter"],
)
def test_a_reasoning_effort_nobody_documented_is_refused_when_the_file_is_read(harness, adapter, effort):
    harness.toml(
        f"""
[providers.p]
label = "P"
adapter = "{adapter}"
retention_note = "n"
[tasks.ping]
description = "d"
data_class = "synthetic"
max_tokens = 8
timeout_seconds = 1
[tasks.ping.models.p]
model = "m"
input_usd_per_mtok = 1.0
output_usd_per_mtok = 1.0
price_checked_at = 2026-11-28
reasoning_effort = "{effort}"
"""
    )
    with pytest.raises(registry.ModelConfigError) as refused:
        registry.table()
    assert "reasoning_effort" in str(refused.value) and effort not in str(refused.value)  # a key, never a value


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
        assert registry.sync(conn) == 6  # anthropic haiku and sonnet, openai luna and sol, local, fake
        conn.execute(
            text(
                "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
                "VALUES ('old', 'm', 'chat', true, 'x')"
            )
        )
        assert registry.sync(conn) == 6
        found = conn.execute(
            text(
                "SELECT provider, model_name, is_active, input_microusd_per_mtok, output_microusd_per_mtok "
                "FROM model_registry"
            )
        )
        rows = {(p, m): (a, i, o) for p, m, a, i, o in found}
    assert rows[("anthropic", "claude-haiku-5-5")] == (True, 100_000, 500_000)
    assert rows[("openai", "gpt-6-luna")] == (True, 100_000, 500_000)
    assert rows[("openai", "gpt-6.2-sol")] == (True, 2_000_000, 10_000_000)
    assert rows[("local", "local-model")] == (True, None, None)
    assert rows[("anthropic", "claude-sonnet-5-5")][0] is True
    assert rows[("old", "m")][0] is False and len(rows) == 7


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
