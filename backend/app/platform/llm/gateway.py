"""The model gateway: one entry point, validated output, one ledger row per provider request (AI_SYSTEM 2, PLT-04)."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from .. import config, db, log, telemetry
from ..config import Role
from . import adapters, fake, registry, stored_key
from .adapters import Reply
from .failures import AIDisabled, ProviderUnavailable, SchemaInvalid

MTOK = 1_000_000
ATTEMPTS = 2  # one fresh call after invalid output, then failure (AI_SYSTEM 2.4)
EMBED_TASK = "embed"
# texts per embeddings request: a failed request is cheap to repeat (OpenAI's own per-request limits were not read)
EMBED_BATCH = 100
EMBED_PROMPT_VERSION = "none"  # llm_calls wants one; an embedding has no prompt
QUERY_TIMEOUT_SECONDS = 4.0  # a search waits this long, once: no SDK retries, then it falls back to keywords

_INSERT_CALL = text(
    "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id, agent_run_id, input_tokens, "
    "output_tokens, cost_microusd, latency_ms, attempt) VALUES (:task, :prompt_version, :model_id, :outcome, :user_id, "
    ":campaign_id, :agent_run_id, :input_tokens, :output_tokens, :cost, :latency_ms, :attempt) RETURNING id"
)


@dataclass
class Attribution:
    """Whose work the model calls inside `attributed` are: the campaign and the agent run they are charged to, and the
    ledger ids of the calls made so far (a `model_call` recorder row stores them, and its cost is their sum)."""

    campaign_id: UUID
    agent_run_id: UUID
    call_ids: list[int] = field(default_factory=list)


# The attribution of the calls being made right now, per thread of execution (a context variable does not cross to a
# thread that was started inside the block, so one worker's campaign can never be charged to another's calls).
_PRINCIPAL: ContextVar[UUID | None] = ContextVar("llm_principal", default=None)
_ATTRIBUTION: ContextVar[Attribution | None] = ContextVar("llm_attribution", default=None)


@contextmanager
def attributed(*, campaign_id: UUID, agent_run_id: UUID) -> Iterator[Attribution]:
    """Charge every model call made inside the block, however deep (match, tailor and apply included), to a campaign and
    an agent run: `llm_calls.campaign_id` and `agent_run_id` are written for each, and the new ids are appended to the
    yielded Attribution's `call_ids`. Outside a block both columns stay NULL."""
    attribution = Attribution(campaign_id, agent_run_id)
    token = _ATTRIBUTION.set(attribution)
    try:
        yield attribution
    finally:
        _ATTRIBUTION.reset(token)


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
    principal = _PRINCIPAL.get()
    if principal is not None and fake.is_registered(principal):
        return fake.call(route.task, principal=principal)
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
    attribution = _ATTRIBUTION.get()
    # on the API login the row must be the signed-in person's own (the candidate_own policy of llm_calls)
    with db.transaction(principal, role=registry.LEDGER.get()) as conn:
        call_id = conn.execute(
            _INSERT_CALL,
            {
                "task": route.task,
                "prompt_version": prompt_version,
                "model_id": model_id,
                "outcome": outcome,
                "user_id": principal,
                "campaign_id": attribution.campaign_id if attribution else None,
                "agent_run_id": attribution.agent_run_id if attribution else None,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cost": cost,
                "latency_ms": latency_ms,
                "attempt": attempt,
            },
        ).scalar_one()
    if attribution is not None:
        attribution.call_ids.append(int(call_id))
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
    ledger: Role = "worker",
) -> BaseModel:
    """Run a task and return its answer validated against schema.

    Raises AIDisabled (nothing configured, nothing called), ProviderUnavailable (timeout or provider failure),
    or SchemaInvalid (invalid twice). Every provider request leaves one llm_calls row, except AIDisabled's.
    `ledger` is the login that writes that row: the worker's, or "api" for a call made in the API process (the chat
    reply), where the row must belong to `principal`.
    """
    token = registry.LEDGER.set(ledger)
    try:
        return _complete(task, schema, messages, principal=principal, prompt_version=prompt_version)
    finally:
        registry.LEDGER.reset(token)


def _complete(
    task: str, schema: type[BaseModel], messages: list[dict[str, Any]], *, principal: UUID | None, prompt_version: str
) -> BaseModel:
    token = _PRINCIPAL.set(principal)  # read by the fake adapter only, so a synthetic candidate replays its own answers
    try:
        return _complete_for(task, schema, messages, principal=principal, prompt_version=prompt_version)
    finally:
        _PRINCIPAL.reset(token)


def _complete_for(
    task: str, schema: type[BaseModel], messages: list[dict[str, Any]], *, principal: UUID | None, prompt_version: str
) -> BaseModel:
    settings = stored_key.apply(config.get())  # the provider named in the span and the log is the one in use
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


def embed_texts(
    texts: list[str], *, principal: UUID | None, ledger: Role = "worker", quick: bool = False
) -> list[list[float]]:
    """One vector per text, in order, from the `embed` route (OpenAI, and only OpenAI: see models.toml).

    The texts go in requests of at most EMBED_BATCH, and every request leaves one llm_calls row with its input
    tokens and their cost, as a chat call does (the texts themselves are never stored). Raises AIDisabled when no
    route or key is configured (nothing is called), or ProviderUnavailable when a request fails: the rows of the
    requests that succeeded before it stay, and so does the failed one's. `quick` is for a person waiting on a search:
    a short timeout and no SDK retries. `ledger` is the login that writes the rows, as in `complete`.
    """
    if not texts:
        return []
    token = registry.LEDGER.set(ledger)
    try:
        return _embed_batches(texts, principal, quick)
    finally:
        registry.LEDGER.reset(token)


def _embed_batches(texts: list[str], principal: UUID | None, quick: bool) -> list[list[float]]:
    settings = config.get()
    try:
        route = registry.route(EMBED_TASK, settings)
    except AIDisabled as disabled:
        log.event(
            "llm.unavailable",
            level=logging.WARNING,
            task=EMBED_TASK,
            provider=settings.llm_provider,
            error_code=disabled.reason,
        )
        raise
    model_id = registry.model_id(route)
    timeout, retries = (QUERY_TIMEOUT_SECONDS, 0) if quick else (route.timeout_seconds, adapters.SDK_RETRIES)
    vectors: list[list[float]] = []
    with telemetry.span("llm.embed", task=EMBED_TASK, provider=route.provider):
        for start in range(0, len(texts), EMBED_BATCH):
            started = time.monotonic()
            try:
                reply = adapters.call_openai_embeddings(
                    route, texts[start : start + EMBED_BATCH], timeout=timeout, retries=retries
                )
            except ProviderUnavailable as failure:
                outcome = "timeout" if failure.timed_out else "provider_error"
                _record(route, model_id, EMBED_PROMPT_VERSION, principal, 1, outcome, None, started)
                raise
            _record(
                route, model_id, EMBED_PROMPT_VERSION, principal, 1, "ok", Reply("", reply.input_tokens, 0), started
            )
            vectors.extend(reply.vectors)
    return vectors
