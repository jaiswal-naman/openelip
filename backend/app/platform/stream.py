"""The per-person live stream, GET /v1/control/stream (API 4.1 and 4.3).

`platform` cannot import the modules above it, so they register a *source* here when they are imported (the composition
root, app.main, imports them). A source has two functions that run on one connection that acts for the caller, in the
threadpool, under row-level security as the API login:

  snapshot(conn, user_id) -> (part of the snapshot event, the source's state)
  poll(conn, user_id, state) -> (new events, the next state)

The server polls the tables every POLL_SECONDS (ARCHITECTURE 9.3: the stream is built from table rows; no LISTEN/NOTIFY,
no Redis). Nothing is buffered or replayed: every connect starts with a snapshot and `Last-Event-ID` is ignored.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, NamedTuple
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import Connection

from . import db, log, sessions
from .errors import ApiError
from .sessions import Principal, current_user

POLL_SECONDS = 2.0  # assumption: "within a few seconds" (API 4.3)
PING_SECONDS = 15  # a comment line when nothing was sent for this long, so proxies keep the connection open
RETRY_MS = 3000  # what EventSource waits before it reconnects

router = APIRouter()

_FAILED = {"code": "internal_error", "message": "The live stream stopped. Reconnecting.", "is_retryable": True}
_SIGNED_OUT = {"code": "unauthenticated", "message": "Sign in to continue.", "is_retryable": False}


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any]
    id: int | None = None  # only recorder.step carries one (the agent_actions id); see API 4.3


Snapshot = Callable[[Connection, UUID], tuple[dict[str, Any], Any]]
Poll = Callable[[Connection, UUID, Any], tuple[list[Event], Any]]
_sources: dict[str, tuple[Snapshot, Poll]] = {}


def source(name: str, *, snapshot: Snapshot, poll: Poll) -> None:
    """Register a source at import time. A second registration under one name is a start-up error."""
    if name in _sources:
        raise RuntimeError(f"stream source {name!r} is registered twice")
    _sources[name] = (snapshot, poll)


class Limits(NamedTuple):
    max_polls: int | None  # None: until the client leaves or the session ends (tests set a number)
    poll_seconds: float
    ping_seconds: float


def limits() -> Limits:
    return Limits(None, POLL_SECONDS, PING_SECONDS)


class _SourceFailed(Exception):
    pass


def _encode(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def frame(event: Event) -> str:
    """One event as API 4.1 writes it. The data is one line of JSON (newlines inside strings are escaped), so stored
    text can never start another field."""
    data = json.dumps(event.data, default=_encode, separators=(",", ":"))
    head = "" if event.id is None else f"id: {event.id}\n"
    return f"{head}event: {event.name}\ndata: {data}\n\n"


def _run(user_id: UUID, step: Callable[[Connection, str, Snapshot, Poll], None]) -> None:
    """Every source in registration order, in one transaction for the caller (row-level security as the API login)."""
    with db.transaction(user_id) as conn:
        for name, (snap, poll) in _sources.items():
            try:
                step(conn, name, snap, poll)
            except Exception as exc:
                log.event("stream.source_failed", level=logging.ERROR, kind=name, error=exc)
                raise _SourceFailed from None


def _read_snapshot(user_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    merged: dict[str, Any] = {}
    states: dict[str, Any] = {}

    def step(conn: Connection, name: str, snap: Snapshot, poll: Poll) -> None:
        part, states[name] = snap(conn, user_id)
        merged.update(part)

    _run(user_id, step)
    return merged, states


def _read_poll(user_id: UUID, states: dict[str, Any]) -> list[Event]:
    events: list[Event] = []

    def step(conn: Connection, name: str, snap: Snapshot, poll: Poll) -> None:
        new, states[name] = poll(conn, user_id, states[name])
        events.extend(new)

    _run(user_id, step)
    return events


async def _frames(request: Request, principal: Principal, digest: bytes, cfg: Limits) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    try:
        merged, states = await run_in_threadpool(_read_snapshot, principal.user_id)
        yield frame(Event("snapshot", merged))
        last_sent = time.monotonic()
        polls = 0
        while cfg.max_polls is None or polls < cfg.max_polls:
            await asyncio.sleep(cfg.poll_seconds)
            if await request.is_disconnected():
                return
            if await run_in_threadpool(sessions._live_session_user, digest) != principal.user_id:
                yield frame(Event("error", _SIGNED_OUT))
                return
            for event in await run_in_threadpool(_read_poll, principal.user_id, states):
                yield frame(event)
                last_sent = time.monotonic()
            polls += 1
            if time.monotonic() - last_sent >= cfg.ping_seconds:
                yield ": ping\n\n"
                last_sent = time.monotonic()
    except _SourceFailed:
        yield frame(Event("error", _FAILED))
    except Exception as exc:  # the database went away, say: one fixed message, nothing from the exception
        log.event("stream.failed", level=logging.ERROR, error=exc)
        yield frame(Event("error", _FAILED))


@router.get("/v1/control/stream")
async def control_stream(
    request: Request, principal: Principal = Depends(current_user), cfg: Limits = Depends(limits)
) -> StreamingResponse:
    """current_user answers 401 with the usual envelope before any stream byte. It runs in the threadpool itself."""
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token is None:  # current_user would have refused already; keeps the type honest
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    return StreamingResponse(
        _frames(request, principal, sessions.token_hash(token), cfg),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
