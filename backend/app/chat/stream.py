"""The reply event stream (plan 05-04; API.md 4.1 and 4.2): what GET .../stream sends, as server-sent events.

A stream starts with `retry: 3000`. Each event is `id:`, `event:` and one `data:` line of JSON, then a blank line. The
first connection to a reply gets every event from message.start (id 1) on. A reconnect (the browser sends the id of the
last event it saw as Last-Event-ID) gets a `message.snapshot` of everything so far, then only what comes after, so no
delta is sent twice and none is lost. A reply the hub no longer has is built from the stored row: a snapshot, then
message.end. The stream closes after message.end. While nothing happens a `: ping` comment is sent every PING_SECONDS so
that a proxy does not close an idle connection.

The generators are async and wait with `await asyncio.sleep`, so a stream that waits holds no worker thread (T-05-26):
200 open streams cost 200 small coroutines. They read memory only (the hub, or a row the route already loaded).
ponytail: polling every POLL_SECONDS. An asyncio event set by the generation thread would remove the delay; switch to it
if the latency of a text.delta is ever measured to matter.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from . import hub
from .service import StoredReply

RETRY_MS = 3000  # assumption (API.md 4.1): how long a browser waits before it reconnects
PING_SECONDS = 15  # assumption (API.md 4.1): quiet this long and a keep-alive comment is sent
POLL_SECONDS = 0.1  # assumption: how often a waiting stream looks at its reply

PREAMBLE = f"retry: {RETRY_MS}\n\n"
PING = ": ping\n\n"
MAX_ID_DIGITS = 9  # a Last-Event-ID longer than this is not one this server gave


def frame(event_id: int | None, name: str, data: dict[str, Any]) -> str:
    """One event. The JSON is compact and ASCII only (non-ASCII text is \\u-escaped), so it is one line whatever the
    text holds: no client, whatever it counts as a line break, can split it."""
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines += [f"event: {name}", "data: " + json.dumps(data, separators=(",", ":"))]
    return "\n".join(lines) + "\n\n"


def parse_last_event_id(value: str | None) -> int | None:
    """The number in a Last-Event-ID header, or None for anything that is not one this server could have sent."""
    text = (value or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= MAX_ID_DIGITS else None


async def from_hub(reply: hub.Reply, after: int | None) -> AsyncIterator[str]:
    """The events of a reply this process holds. `after` is the client's Last-Event-ID: None for a first connection (the
    whole reply is replayed), a number for a reconnect (a snapshot, then what comes after it)."""
    yield PREAMBLE
    sent = 0
    if after is not None:
        sent, state = reply.snapshot()
        yield frame(sent, "message.snapshot", state)
    quiet_since = time.monotonic()
    while True:
        done = (
            reply.finished.is_set()
        )  # read before the events: whatever was emitted before finish() is then seen below
        events = reply.since(sent)
        for event in events:
            yield frame(event.id, event.name, event.data)
            sent = event.id
            if event.name == "message.end":
                return
        if events:
            quiet_since = time.monotonic()
            continue
        if done:
            return  # ended without a message.end: nothing more will come
        if time.monotonic() - quiet_since >= PING_SECONDS:
            yield PING
            quiet_since = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)


async def from_row(stored: StoredReply) -> AsyncIterator[str]:
    """A finished reply the hub no longer has: a snapshot of its stored text, citations and proposals, then message.end
    with the id and usage that were stored with it (usage is null for a reply that was ended by reading it)."""
    refs = stored.refs
    yield PREAMBLE
    state = {"text": stored.content, "citations": refs.get("citations", []), "proposals": refs.get("proposals", [])}
    if refs.get("campaign_proposals"):
        state["campaign_proposals"] = refs["campaign_proposals"]
    yield frame(None, "message.snapshot", state)
    end = {
        "message_id": str(stored.id),
        "finish_reason": stored.finish_reason,
        "usage": refs.get("usage"),
        "cost_usd": refs.get("cost_usd"),
    }
    yield frame(refs.get("last_event_id"), "message.end", end)
