"""The real adapters against stub clients that return the SDKs' own response and error objects. No network."""

import importlib
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from app.platform import config
from app.platform.llm import ProviderUnavailable, adapters, registry
from app.platform.llm.__main__ import PingResult

SCHEMA = PingResult.model_json_schema()
REQUEST = httpx2.Request("POST", "https://provider.example/v1")


def route(**env: str) -> registry.Route:
    return registry.route("ping", config.load({"APP_ENV": "test", **env}))


def stub(monkeypatch, sdk, name, outcome):
    """Replace the SDK's client class; the stub records its constructor and request arguments."""
    seen: dict = {}

    class Client:
        def __init__(self, **kwargs):
            seen["init"] = kwargs
            create = self.create
            self.messages = SimpleNamespace(create=create)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **kwargs):
            seen["request"] = kwargs
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(sdk, name, Client)
    return seen


def message(text='{"reply": "pong"}', stop_reason="end_turn"):
    block = anthropic.types.TextBlock.model_construct(type="text", text=text)
    usage = anthropic.types.Usage.model_construct(input_tokens=7, output_tokens=3)
    return anthropic.types.Message.model_construct(content=[block], usage=usage, stop_reason=stop_reason)


def completion(content='{"reply": "pong"}', refusal=None):
    reply = ChatCompletionMessage.model_construct(content=content, refusal=refusal)
    usage = CompletionUsage.model_construct(prompt_tokens=7, completion_tokens=3)
    return ChatCompletion.model_construct(choices=[Choice.model_construct(message=reply)], usage=usage)


def test_anthropic_sends_the_schema_and_returns_text_and_tokens(monkeypatch):
    seen = stub(monkeypatch, anthropic, "Anthropic", message())
    messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    found = adapters.call_anthropic(route(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k"), messages, SCHEMA)
    assert found == adapters.Reply('{"reply": "pong"}', 7, 3, False)
    assert seen["init"] == {"api_key": "k", "timeout": 30.0, "max_retries": 2, "open_telemetry": False}
    request = seen["request"]
    assert request["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert (request["model"], request["max_tokens"], request["system"]) == ("claude-haiku-5-5", 512, "be brief")
    assert request["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in request  # Sonnet 5.5 rejects sampling parameters


def test_an_anthropic_refusal_is_flagged(monkeypatch):
    stub(monkeypatch, anthropic, "Anthropic", message("", stop_reason="refusal"))
    assert adapters.call_anthropic(route(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k"), [], SCHEMA).refused


@pytest.mark.parametrize(
    ("sdk", "name", "env"),
    [(anthropic, "Anthropic", {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}),
     (openai, "OpenAI", {"LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "k"})],
)  # fmt: skip
@pytest.mark.parametrize("error", ["timeout", "status"])
def test_sdk_errors_become_provider_unavailable_without_their_text(monkeypatch, sdk, name, env, error):
    boom = (
        sdk.APITimeoutError(request=REQUEST)
        if error == "timeout"
        else sdk.InternalServerError("CANARY quoted request", response=httpx2.Response(500, request=REQUEST), body=None)
    )
    stub(monkeypatch, sdk, name, boom)
    call = adapters.call_anthropic if sdk is anthropic else adapters.call_openai_compatible
    with pytest.raises(ProviderUnavailable) as raised:
        call(route(**env), [], SCHEMA)
    assert raised.value.timed_out is (error == "timeout") and "CANARY" not in str(raised.value.message)


def test_openai_gets_a_strict_json_schema_and_the_current_token_limit(monkeypatch):
    seen = stub(monkeypatch, openai, "OpenAI", completion())
    found = adapters.call_openai_compatible(route(LLM_PROVIDER="openai_compatible", OPENAI_API_KEY="k"), [], SCHEMA)
    assert found == adapters.Reply('{"reply": "pong"}', 7, 3, False)
    assert seen["init"]["base_url"] == "https://api.openai.com/v1" and seen["init"]["api_key"] == "k"
    request = seen["request"]
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "PingResult", "schema": SCHEMA, "strict": True},
    }
    assert request["max_completion_tokens"] == 512 and "max_tokens" not in request


def test_a_local_server_gets_json_mode_the_older_token_limit_and_a_placeholder_key(monkeypatch):
    seen = stub(monkeypatch, openai, "OpenAI", completion(refusal="no"))
    env = {"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://llm:11434/v1"}
    found = adapters.call_openai_compatible(route(**env), [], SCHEMA)
    assert found.refused and seen["init"]["api_key"] == "not-needed"
    assert seen["request"]["response_format"] == {"type": "json_object"}
    assert seen["request"]["max_tokens"] == 512 and "max_completion_tokens" not in seen["request"]


# Every schema a task hands the gateway (AI_SYSTEM 2.3), by the module that defines it.
TASK_SCHEMAS = [
    ("app.platform.llm.__main__", "PingResult"),
    ("app.profile.extract.schemas", "RoleItems"),
    ("app.profile.extract.schemas", "EducationSkillItems"),
    ("app.profile.extract.schemas", "StatementItems"),
    ("app.ingest.requirements.schema", "RequirementList"),
    ("app.match.validator", "VerdictSet"),
    ("app.chat.schemas", "ChatStep"),
    ("app.chat.schemas", "ProposalList"),
    ("app.tailor.schemas", "EditList"),
    ("app.tailor.schemas", "EditListSafe"),
    ("app.tailor.schemas", "EntailSet"),
    ("app.apply.packet", "PacketDraft"),
    ("app.campaigns.crew.research", "PlanStep"),
    ("app.campaigns.crew.research", "PageClaims"),
    ("app.campaigns.crew.chaser", "MessageDraft"),
    ("app.campaigns.crew.coach", "PrepPack"),
    ("app.twin.schemas", "TopicClassification"),
    ("app.twin.schemas", "TwinAnswer"),
]


def strict_problems(node, path="#"):
    """Where a schema breaks OpenAI's strict structured-output rules: every object closed with additionalProperties
    false, every property listed in required, and no default (the structured-outputs guide, read 2026-11-28)."""
    if isinstance(node, list):
        return [p for i, item in enumerate(node) for p in strict_problems(item, f"{path}[{i}]")]
    if not isinstance(node, dict):
        return []
    found = []
    if "properties" in node:
        if node.get("additionalProperties") is not False:
            found.append(f"{path}: object not closed")
        if set(node.get("required", [])) != set(node["properties"]):
            found.append(f"{path}: not every property is required")
    if "default" in node:
        found.append(f"{path}: default")
    for key, value in node.items():
        if key in ("properties", "$defs"):  # a map of names to schemas: the names are not keywords
            found += [p for name, sub in value.items() for p in strict_problems(sub, f"{path}/{key}/{name}")]
        else:
            found += strict_problems(value, f"{path}/{key}")
    return found


@pytest.mark.parametrize(("module", "name"), TASK_SCHEMAS, ids=[name for _, name in TASK_SCHEMAS])
def test_openai_gets_every_task_schema_in_strict_form(monkeypatch, module, name):
    schema = getattr(importlib.import_module(module), name).model_json_schema()
    seen = stub(monkeypatch, openai, "OpenAI", completion())
    adapters.call_openai_compatible(route(LLM_PROVIDER="openai_compatible", OPENAI_API_KEY="k"), [], schema)
    sent = seen["request"]["response_format"]["json_schema"]
    assert sent["strict"] is True and strict_problems(sent["schema"]) == []


def test_a_property_named_default_or_required_survives_the_strict_form(monkeypatch):
    schema = {"type": "object", "properties": {"default": {"type": "string"}, "required": {"type": "integer"}}}
    seen = stub(monkeypatch, openai, "OpenAI", completion())
    adapters.call_openai_compatible(route(LLM_PROVIDER="openai_compatible", OPENAI_API_KEY="k"), [], schema)
    sent = seen["request"]["response_format"]["json_schema"]["schema"]
    assert set(sent["properties"]) == {"default", "required"} and sent["required"] == ["default", "required"]
    assert sent["additionalProperties"] is False and schema == {
        "type": "object",
        "properties": {"default": {"type": "string"}, "required": {"type": "integer"}},
    }  # the caller's schema is left as it was


def test_openai_is_sent_the_reasoning_effort_its_route_names(monkeypatch):
    seen = stub(monkeypatch, openai, "OpenAI", completion())
    chosen = replace(route(LLM_PROVIDER="openai_compatible", OPENAI_API_KEY="k"), reasoning_effort="low")
    adapters.call_openai_compatible(chosen, [], SCHEMA)
    assert seen["request"]["reasoning_effort"] == "low"


def test_no_reasoning_effort_is_sent_when_the_route_names_none(monkeypatch):
    seen = stub(monkeypatch, openai, "OpenAI", completion())
    local = route(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="http://llm:11434/v1")
    assert local.reasoning_effort is None
    adapters.call_openai_compatible(local, [], SCHEMA)
    assert "reasoning_effort" not in seen["request"]
