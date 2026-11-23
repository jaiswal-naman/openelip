"""The reply hub (plan 05-04; API.md 4.2): the events of the replies this process is writing, or finished a moment ago.

A reply is generated in a background thread of the API process (decision 3), whether or not anyone is watching. The
thread appends each event here under the next number of its reply (1, 2, 3, ...); a stream reads from here, and so does
a reconnect, which is why a dropped connection loses nothing. A finished reply stays for HUB_KEEP_SECONDS so that a
reconnect finds its events in memory; after that the database holds the reply and the stream is built from the stored
row (stream.py). Nothing here touches the database.

ponytail: one API process. The hub is a dict in memory, so a second API process would not see the first one's replies,
the same ceiling the sign-in address limiter has. Move generation to the worker with a shared event store (a table, or
Postgres LISTEN/NOTIFY) if a second API process is ever added.
"""

import threading
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

HUB_KEEP_SECONDS = 300  # assumption: a finished reply stays this long, for a reconnect to find its events


@dataclass(frozen=True)
class Event:
    id: int  # 1 for message.start, then one more for each event of the reply
    name: str
    data: dict[str, Any]


class Reply:
    """One reply: its events in order, and the two flags that cross threads. The generation thread writes it; any number
    of streams read it."""

    def __init__(self, message_id: UUID, session_id: UUID, user_id: UUID) -> None:
        self.message_id = message_id
        self.session_id = session_id
        self.user_id = user_id
        self.finished = threading.Event()  # set when the reply has ended and been stored
        self.ended_at: float | None = None
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._stop = False

    def emit(self, name: str, data: dict[str, Any]) -> Event:
        with self._lock:
            event = Event(len(self._events) + 1, name, data)
            self._events.append(event)
        return event

    @property
    def last_id(self) -> int:
        with self._lock:
            return len(self._events)

    def since(self, after: int) -> list[Event]:
        """The events with an id above `after`, in order. An id is its place in the list, counted from 1."""
        with self._lock:
            return self._events[max(after, 0) :]

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        """(the id the snapshot is as of, what a client that missed events needs: the text so far, the citations and the
        proposals). A closing message.end is not part of it, so the stream still sends that after the snapshot."""
        with self._lock:
            events = list(self._events)
        if events and events[-1].name == "message.end":
            events.pop()
        state = {
            "text": "".join(e.data["text"] for e in events if e.name == "text.delta"),
            "citations": [e.data for e in events if e.name == "citation"],
            "proposals": [e.data for e in events if e.name == "proposal"],
        }
        offers = [e.data for e in events if e.name == "campaign_proposal"]
        if offers:  # only a reply that offered a campaign carries the key, so the other snapshots keep their shape
            state["campaign_proposals"] = offers
        return len(events), state

    def request_stop(self) -> None:
        self._stop = True

    def stopping(self) -> bool:
        return self._stop

    def finish(self) -> None:
        self.ended_at = time.monotonic()
        self.finished.set()


_replies: dict[UUID, Reply] = {}
_lock = threading.Lock()


def _evict() -> None:
    """Forget the replies that ended HUB_KEEP_SECONDS ago. Called with the lock held, on every visit."""
    now = time.monotonic()
    for key in [k for k, r in _replies.items() if r.ended_at is not None and now - r.ended_at >= HUB_KEEP_SECONDS]:
        del _replies[key]


def start(message_id: UUID, session_id: UUID, user_id: UUID) -> Reply:
    """A new reply, already holding its message.start (event 1)."""
    reply = Reply(message_id, session_id, user_id)
    reply.emit("message.start", {"message_id": str(message_id), "session_id": str(session_id)})
    with _lock:
        _evict()
        _replies[message_id] = reply
    return reply


def get(message_id: UUID) -> Reply | None:
    with _lock:
        _evict()
        return _replies.get(message_id)


def is_running(message_id: UUID) -> bool:
    reply = get(message_id)
    return reply is not None and not reply.finished.is_set()


def running() -> list[Reply]:
    with _lock:
        return [reply for reply in _replies.values() if not reply.finished.is_set()]


def discard(message_id: UUID) -> None:
    """Forget a reply whose acceptance did not commit."""
    with _lock:
        _replies.pop(message_id, None)
