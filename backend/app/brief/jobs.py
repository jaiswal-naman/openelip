"""Queue handlers of the brief module: brief.sweep, which every minute finds the people whose brief is due, and
brief.compose, which composes one person's brief for one local date (ARCHITECTURE 4.3; INDEX deviation 21).

The worker imports this module, so the handlers exist before the first job is claimed, and so does the email template:
importing it registers brief_v1 with the relay (the platform may not import the brief, so the brief reaches down).

A person is due on a local date when it is past 05:30 for them, their brief is not paused (or weekly on another day),
they have no in-app brief for the date yet, their record was confirmed before this morning's 05:30 (someone who
confirms it later has their first brief tomorrow, as the onboarding page says), and their nightly match run is not
still queued or running, unless it is past 06:15, when the brief is composed with whatever exists. A run that failed
is not waited for.
"""

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.match import api as match
from app.platform import log, queue, relay, scheduler

from . import mail
from .compose import SWEEP_START, WAIT_UNTIL, compose

SWEEP_EVERY_SECONDS = 60
COMPOSE_JOB = "brief.compose"

relay.RESOLVERS["brief_v1"] = mail.resolve


def due_people(conn: Connection, now: datetime) -> list[tuple[UUID, date]]:
    """Who needs a brief composed at `now` (an aware datetime), each with the local date it is for.

    ponytail: reads every unpaused person each minute, which is fine for thousands; plan per time zone (the
    users_due_idx index) if the count ever shows in the database.
    """
    people = conn.execute(
        text(
            """SELECT id, timezone, brief_frequency::text AS frequency, brief_weekday, record_confirmed_at FROM users
               WHERE status = 'active' AND record_confirmed_at IS NOT NULL AND brief_frequency <> 'paused'"""
        )
    ).all()
    candidates: list[tuple[UUID, datetime]] = []
    for person in people:
        try:
            local = now.astimezone(ZoneInfo(person.timezone))
        except (KeyError, ValueError, OSError):  # a zone this machine does not know: skip the person, not the sweep
            continue
        window = datetime.combine(local.date(), SWEEP_START, tzinfo=local.tzinfo)
        if local < window or person.record_confirmed_at >= window:
            continue
        if person.frequency == "weekly" and local.isoweekday() != person.brief_weekday:
            continue
        candidates.append((person.id, local))
    if not candidates:
        return []
    done = set(
        conn.execute(
            text(
                """SELECT b.user_id FROM briefs b
                   JOIN unnest(CAST(:ids AS uuid[]), CAST(:dates AS date[])) AS t(user_id, local_date)
                     ON t.user_id = b.user_id AND t.local_date = b.local_date
                   WHERE b.channel = 'in_app'"""
            ),
            {"ids": [user_id for user_id, _ in candidates], "dates": [local.date() for _, local in candidates]},
        ).scalars()
    )
    due = []
    for user_id, local in candidates:
        if user_id in done:
            continue
        still_running = match.nightly_status(conn, user_id, local.date()) in ("queued", "running")
        if still_running and local.time() < WAIT_UNTIL:
            continue
        due.append((user_id, local.date()))
    return due


def queue_composes(conn: Connection, now: datetime) -> int:
    """Queue one brief.compose for each person due at `now`; returns how many were new. The queue's unique key
    brief.compose:{user}:{date} makes a repeat of the same minute, or of the next one, add nothing."""
    queued = sum(
        queue.enqueue(
            conn,
            COMPOSE_JOB,
            {"user_id": str(user_id), "local_date": local_date.isoformat()},
            unique_key=f"{COMPOSE_JOB}:{user_id}:{local_date}",
        )
        is not None
        for user_id, local_date in due_people(conn, now)
    )
    if queued:
        log.event("brief.sweep", queued=queued)
    return queued


@scheduler.every("brief.sweep", SWEEP_EVERY_SECONDS)
def sweep(conn: Connection, payload: dict[str, Any]) -> None:
    queue_composes(conn, datetime.now(timezone.utc))


@queue.handler(COMPOSE_JOB)
def compose_one(conn: Connection, payload: dict[str, Any]) -> None:
    compose(conn, UUID(payload["user_id"]), date.fromisoformat(payload["local_date"]), now=datetime.now(timezone.utc))
