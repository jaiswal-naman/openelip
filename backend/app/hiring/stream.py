"""The hiring source of the live stream (API 4.3): `introduction.status`. It has no snapshot key.

The events are built from the same list views `GET /v1/introductions` uses: `list_for_candidate` for the caller's own
introductions and `list_for_employer` for each organization they are an active member of. So an employer member is told
exactly what their list shows: accepted introductions only, and nothing of a candidate's private decline.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from app.platform import stream
from app.platform.stream import Event

from . import introductions, queries

PAGE = 50  # assumption: the newest introductions of each list are followed; ponytail: page further for a busy desk

State = dict[UUID, tuple[Any, ...]]


def _current(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """Every introduction the caller's lists show now: their own, then each organization's accepted ones."""
    items, _ = introductions.list_for_candidate(conn, user_id, limit=PAGE, cursor=None)
    memberships, _ = queries.my_organizations(conn, user_id)
    for org in memberships:
        found, _ = introductions.list_for_employer(
            conn, user_id, role_id=None, org_id=org.id, status=None, limit=PAGE, cursor=None
        )
        items.extend(found)
    return items


def _report(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item["status"], item["employer_answer"], item.get("answer_due_at"))  # a candidate's list has no due time


def snapshot(conn: Connection, user_id: UUID) -> tuple[dict[str, Any], State]:
    return {}, {item["id"]: _report(item) for item in _current(conn, user_id)}


def poll(conn: Connection, user_id: UUID, state: State) -> tuple[list[Event], State]:
    events = []
    for item in _current(conn, user_id):
        report = _report(item)
        if state.get(item["id"]) != report:
            state[item["id"]] = report
            events.append(
                Event(
                    "introduction.status",
                    {
                        "introduction_id": item["id"],
                        "status": report[0],
                        "employer_answer": report[1],
                        "answer_due_at": report[2],
                    },
                )
            )
    return events, state


stream.source("hiring", snapshot=snapshot, poll=poll)
