"""The two provider adapters: the only code that imports a model provider's SDK (ARCHITECTURE 3.3, AI_SYSTEM 2.11).

Each takes a resolved Route, the messages and the output's JSON schema and returns what the model wrote with its token
counts. An SDK is imported inside its function: loading one takes seconds and most processes never call a provider.
SDK errors become ProviderUnavailable with no message, because an SDK error can quote the request.
"""

from dataclasses import dataclass
from typing import Any, cast

from .failures import ProviderUnavailable
from .registry import Route

SDK_RETRIES = 2  # the SDK's own backoff on rate limits and 5xx (AI_SYSTEM 2.4); the gateway adds no second layer


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    refused: bool = False


def call_anthropic(route: Route, messages: list[dict[str, Any]], schema: dict[str, object]) -> Reply:
    import anthropic

    system = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    try:
        # open_telemetry=False: the SDK would emit its own spans by default, and an environment variable can put the
        # prompt and the answer into them (PLT-07). The gateway's own spans carry ids only.
        with anthropic.Anthropic(
            api_key=route.api_key, timeout=route.timeout_seconds, max_retries=SDK_RETRIES, open_telemetry=False
        ) as client:
            message = client.messages.create(
                model=route.model,
                max_tokens=route.max_tokens,
                system=system or anthropic.omit,
                messages=cast(Any, turns),
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
    except anthropic.APITimeoutError:
        raise ProviderUnavailable(timed_out=True) from None
    except anthropic.APIError:
        raise ProviderUnavailable() from None
    text = "".join(block.text for block in message.content if isinstance(block, anthropic.types.TextBlock))
    return Reply(text, message.usage.input_tokens, message.usage.output_tokens, message.stop_reason == "refusal")


def _strict(node: Any) -> Any:
    """A copy of a JSON schema in the form OpenAI's strict structured outputs accept: every object closed, every
    property required, no `default`. Pydantic leaves a field that has a default out of `required`, and OpenAI answers
    a schema like that with a 400. A model that must give every field gives that field its default (a nullable type
    stays nullable), and the task's own Pydantic class still validates what comes back."""
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, dict):
        return node
    strict = {key: _strict(value) for key, value in node.items() if key not in ("default", "properties", "$defs")}
    for key in ("properties", "$defs"):  # maps of names to schemas: a property may be called "default"
        if key in node:
            strict[key] = {name: _strict(sub) for name, sub in node[key].items()}
    if "properties" in node:
        strict["additionalProperties"] = False
        strict["required"] = list(node["properties"])
    return strict


def call_openai_compatible(route: Route, messages: list[dict[str, Any]], schema: dict[str, object]) -> Reply:
    import openai

    response_format: dict[str, Any] = {"type": "json_object"}  # JSON mode: the prompt itself must ask for JSON
    if route.structured_output == "json_schema":
        name = str(schema.get("title", "result"))
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": _strict(schema), "strict": True},
        }
    request: dict[str, Any] = {
        "model": route.model,
        "messages": messages,
        "response_format": response_format,
        route.token_limit_param: route.max_tokens,
    }
    if route.reasoning_effort:  # the Chat Completions parameter; reasoning tokens count against the limit above
        request["reasoning_effort"] = route.reasoning_effort
    try:
        with openai.OpenAI(
            api_key=route.api_key, base_url=route.base_url, timeout=route.timeout_seconds, max_retries=SDK_RETRIES
        ) as client:
            completion = client.chat.completions.create(**request)
    except openai.APITimeoutError:
        raise ProviderUnavailable(timed_out=True) from None
    except openai.APIError:
        raise ProviderUnavailable() from None
    message = completion.choices[0].message if completion.choices else None
    usage = completion.usage  # a local server may omit it; then the tokens are unknown and recorded as 0
    return Reply(
        (message.content if message else None) or "",
        usage.prompt_tokens if usage else 0,
        usage.completion_tokens if usage else 0,
        bool(message and message.refusal),
    )
