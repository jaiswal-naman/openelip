"""The model gateway: one entry point, validated output, one ledger row per provider request (AI_SYSTEM 2, PLT-04)."""

import logging
import time
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from .. import config, db, log, telemetry
from . import adapters, fake, registry
from .adapters import Reply
from .failures import AIDisabled, ProviderUnavailable, SchemaInvalid

MTOK = 1_000_000
ATTEMPTS = 2  # one fresh call after invalid output, then failure (AI_SYSTEM 2.4)

_INSERT_CALL = text(
    "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, input_tokens, output_tokens, "
    "cost_microusd, latency_ms, attempt) VALUES (:task, :prompt_version, :model_id, :outcome, :user_id, "
    ":input_tokens, :output_tokens, :cost, :latency_ms, :attempt)"
)


def cost_microusd(input_tokens: int, output_tokens: int, input_price: int | None, output_price: int | None) -> int:
    """Whole micro-dollars, rounded half up, in integers so sums never drift. Prices are micro-dollars per million
    tokens; a local model has none, and its calls cost nothing per token."""
    if input_price is None or output_price is None:
        return 0
    return (input_tokens * input_price + output_tokens * output_price + MTOK // 2) // MTOK


def usd(micro: int) -> str:
    """Micro-dollars as the API's decimal string with six places (API.md 1.4)."""
    return f"{micro // MTOK}.{micro % MTOK:06d}"


def _call(route: registry.Route, messages: list[dict[str, Any]], schema: type[BaseModel]) -> Reply:
    if route.adapter == "anthropic":
        return adapters.call_anthropic(route, messages, schema.model_json_schema())
    if route.adapter == "openai_compatible":
        return adapters.call_openai_compatible(route, messages, schema.model_json_schema())
    return fake.call(route.task)


def _record(
    route: registry.Route,
    model_id: UUID,
    prompt_version: str,
    principal: UUID | None,
    attempt: int,
    outcome: str,
    reply: Reply | None,
    started: float,
) -> None:
    """One ledger row, on its own connection so a caller that rolls back still leaves it. Numbers and ids only."""
    tokens_in, tokens_out = (reply.input_tokens, reply.output_tokens) if reply else (0, 0)
    cost = cost_microusd(tokens_in, tokens_out, route.input_microusd_per_mtok, route.output_microusd_per_mtok)
    latency_ms = int((time.monotonic() - started) * 1000)
    with db.transaction(role="worker") as conn:
        conn.execute(
            _INSERT_CALL,
            {
                "task": route.task,
                "prompt_version": prompt_version,
                "model_id": model_id,
                "outcome": outcome,
                "user_id": principal,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cost": cost,
                "latency_ms": latency_ms,
                "attempt": attempt,
            },
        )
    level = logging.INFO if outcome == "ok" else logging.WARNING
    log.event(
        "llm.call",
        level=level,
        task=route.task,
        provider=route.provider,
        model=route.model,
        outcome=outcome,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        latency_ms=latency_ms,
        attempt=attempt,
    )


def complete(
    task: str,
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    *,
    principal: UUID | None,
    prompt_version: str,
) -> BaseModel:
    """Run a task and return its answer validated against schema.

    Raises AIDisabled (nothing configured, nothing called), ProviderUnavailable (timeout or provider failure),
    or SchemaInvalid (invalid twice). Every provider request leaves one llm_calls row, except AIDisabled's.
    """
    settings = config.get()
    with telemetry.span("llm.complete", task=task, provider=settings.llm_provider):
        try:
            route = registry.route(task, settings)
        except AIDisabled as disabled:
            log.event(
                "llm.unavailable",
                level=logging.WARNING,
                task=task,
                provider=settings.llm_provider,
                error_code=disabled.reason,
            )
            raise
        # ponytail: Phase 3 requires the ai_processing consent here (trust.consent.require). platform cannot import
        # trust, so by then the caller will pass the checked consent in.
        model_id = registry.model_id(route)
        outcome = "schema_invalid"
        for attempt in range(1, ATTEMPTS + 1):
            started = time.monotonic()
            try:
                reply = _call(route, messages, schema)
            except ProviderUnavailable as failure:
                _record(
                    route,
                    model_id,
                    prompt_version,
                    principal,
                    attempt,
                    "timeout" if failure.timed_out else "provider_error",
                    None,
                    started,
                )
                raise
            result: BaseModel | None = None
            if reply.refused:
                outcome = "refused"
            else:
                try:
                    result = schema.model_validate_json(reply.text)
                    outcome = "ok"
                except ValidationError:  # its message quotes the input: caught here, never logged, never chained
                    outcome = "schema_invalid"
            _record(route, model_id, prompt_version, principal, attempt, outcome, reply, started)
            if result is not None:
                return result
        raise SchemaInvalid(refused=outcome == "refused")
