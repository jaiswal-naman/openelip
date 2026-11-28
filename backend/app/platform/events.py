"""Domain events (ARCHITECTURE 3.3): how a lower module wakes a higher one it may not import.

A module that wants to hear about an event subscribes at import time, naming the queue job kind that should run
(subscribe fills a start-up registry, in each process that imports the module). The module that causes the event calls
publish in its own transaction: one queue job per subscriber is inserted there, so the jobs exist if and only if the
cause committed, and a repeated publish of the same key adds nothing (the queue's unique key). Nothing is delivered in
the publisher's process: the worker runs the jobs.

Events and their payloads are listed in the Phase 7 index; a payload holds ids, codes and numbers only.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from . import queue

# event name -> the (job kind, queue) of every subscriber, in the order they subscribed
_subscribers: dict[str, list[tuple[str, str]]] = {}


def subscribe(event: str, kind: str, *, queue: str = "default") -> None:
    """Run the queue job `kind` whenever `event` is published. Subscribing the same pair twice counts once."""
    pair = (kind, queue)
    entries = _subscribers.setdefault(event, [])
    if pair not in entries:
        entries.append(pair)


def subscribers(event: str) -> list[tuple[str, str]]:
    """The (job kind, queue) pairs subscribed to the event, for tests and for the start-up checks."""
    return list(_subscribers.get(event, []))


def publish(conn: Connection, event: str, payload: dict[str, Any], *, key: str, user_id: UUID | None = None) -> int:
    """Enqueue one job per subscriber, in the caller's transaction, and return how many it tried to add (0 when nobody
    subscribed). The job's unique key is `{kind}:{key}`, so publishing the same key again changes nothing.

    The job names the person it is about, so erasing the person deletes it: `user_id` when given, else the payload's
    own `user_id` when it has one."""
    owner = user_id
    if owner is None and payload.get("user_id") is not None:
        owner = UUID(str(payload["user_id"]))
    attempted = 0
    for kind, queue_name in _subscribers.get(event, []):
        queue.enqueue(conn, kind, payload, unique_key=f"{kind}:{key}", queue=queue_name, user_id=owner)
        attempted += 1
    return attempted
