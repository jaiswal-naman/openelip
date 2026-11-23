"""The Twin's source of the live stream (API 4.3): `twin.message` for every new message the caller may read.

No snapshot key: the page reads the conversation over HTTP and the stream only says that something new arrived. Messages
are read on the API login for the caller, so row-level security decides who hears of what: the candidate hears of every
question, Twin message and reply of their sessions; an employer member hears of questions and Twin messages of their
organization and never of a candidate's reply (the employer policy hides those rows).

Rows commit out of order by a little, so the source reads everything newer than a time a minute before its baseline and
remembers the ids it already announced (the same approach as the Guardian's source).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import stream
from app.platform.stream import Event

SLACK = timedelta(seconds=60)  # assumption: a message commits less than this long after its created_at

_MESSAGES = """SELECT id, session_id, author::text AS author, answer_kind::text AS answer_kind, created_at
                 FROM twin_messages WHERE created_at >= :since ORDER BY created_at, id"""


@dataclass
class State:
    since: datetime  # messages created after this are looked at
    seen: set[UUID] = field(default_factory=set)  # ids announced, or present at the baseline, above `since`


def snapshot(conn: Connection, user_id: UUID) -> tuple[dict[str, Any], State]:
    now = conn.execute(text("SELECT now()")).scalar_one()
    state = State(now - SLACK)
    state.seen.update(row.id for row in conn.execute(text(_MESSAGES), {"since": state.since}))
    return {}, state


def poll(conn: Connection, user_id: UUID, state: State) -> tuple[list[Event], State]:
    events: list[Event] = []
    newest = state.since
    for row in conn.execute(text(_MESSAGES), {"since": state.since}):
        newest = max(newest, row.created_at - SLACK)
        if row.id in state.seen:
            continue
        state.seen.add(row.id)
        events.append(
            Event(
                "twin.message",
                {
                    "session_id": row.session_id,
                    "message_id": row.id,
                    "author": row.author,
                    "answer_kind": row.answer_kind,
                },
            )
        )
    if newest > state.since:  # forget ids that can no longer be returned
        state.since = newest
    return events, state


stream.source("twin", snapshot=snapshot, poll=poll)
