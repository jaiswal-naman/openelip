"""Scheduled jobs (ARCHITECTURE 5.3).

@every(name, seconds) marks a handler as recurring. The worker calls tick() about once a second; tick() enqueues each
entry once per period, in a job named {name}:{slot}, where slot is the start of the current period in epoch seconds. The
queue's unique key makes that safe with any number of workers ticking: the second one to insert a slot finds it taken.

The decorated function is also registered as the queue handler for jobs of kind `name`, so a schedule entry can never
exist without something to run it.

ponytail: tick() runs one INSERT per entry per call and lets the unique key discard it. Remember the last slot in
memory if the entries ever grow beyond a handful.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Connection

from . import queue

F = TypeVar("F", bound=queue.Handler)

_every: dict[str, int] = {}


def every(name: str, seconds: int) -> Callable[[F], F]:
    """Decorator: run fn(conn, payload) once per `seconds`, as the job of kind `name`."""

    def register(fn: F) -> F:
        if name in _every:
            raise ValueError(f"{name!r} is scheduled twice")
        _every[name] = seconds
        queue.handler(name)(fn)
        return fn

    return register


def slot_of(now: datetime, seconds: int) -> int:
    """The start of the period that contains `now` (an aware datetime), in epoch seconds."""
    epoch = int(now.timestamp())
    return epoch - epoch % seconds


def tick(conn: Connection, now: datetime) -> int:
    """Enqueue every entry whose current slot has no job yet; returns how many this call enqueued."""
    enqueued = 0
    for name, seconds in _every.items():
        if queue.enqueue(conn, name, {}, unique_key=f"{name}:{slot_of(now, seconds)}") is not None:
            enqueued += 1
    return enqueued


# The queue's own upkeep is scheduled like any other job. It lives here, not in queue.py, so queue.py need not import
# this module (which imports it).
@every("queue.reap", 60)
def _reap(conn: Connection, payload: dict[str, Any]) -> None:
    queue.reap(conn)


@every("queue.prune", 86400)
def _prune(conn: Connection, payload: dict[str, Any]) -> None:
    queue.prune(conn)
