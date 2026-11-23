"""Compose one person's morning brief for one local date (BRF-01, BRF-02; ARCHITECTURE 4.3).

compose() is what the brief.compose job and the demo seed call. In the caller's transaction it writes the in-app brief,
its items and, when the person has agreed to the email (the brief_email consent), the email brief and its outbox row,
all or nothing. Running it again for the same person and date changes nothing and returns the ids that exist: the
database holds the guard (briefs_one_per_day, and the outbox dedupe key brief:{email brief id}).

The apply links of the matches are asked before any brief row is written, so no brief is held open across the network.
ponytail: verify_live's own UPDATE of a job lives until this transaction ends; five rows at most, and an ingest refresh
of one of them waits rather than fails. Check each link in a transaction of its own if that ever shows in a log.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from random import Random
from typing import Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.hiring import api as hiring
from app.ingest import api as ingest
from app.match import api as match
from app.platform import log, outbox
from app.profile import api as profile
from app.trust import api as trust

# The morning, in the person's own time zone. Assumptions, to be tuned from the first real mornings (INDEX check 7).
SWEEP_START = time(5, 30)  # assumption: a brief is composed from here on, once the night's run has had its time
WAIT_UNTIL = time(6, 15)  # assumption: a nightly run still queued or running after this is no longer waited for
SEND_FROM = time(6, 30)  # assumption: the earliest the email is released
JITTER_MINUTES = 25  # assumption: releases spread over 06:30 to 06:55, so the relay never faces one burst
# assumption: an email that missed its slot is dropped at local noon, never sent in the afternoon
EXPIRES_AT = time(12, 0)
EMAIL_TEMPLATE = "brief_v1"

ComposeStatus = Literal["composed", "suppressed"]


@dataclass(frozen=True)
class ComposeResult:
    """The ids of what exists for the person and date. status is the in-app brief's: "suppressed" is a day with nothing
    to say (no match, no proposal waiting, nothing done), which sends no email."""

    in_app_brief_id: UUID
    email_brief_id: UUID | None
    outbox_id: UUID | None
    status: ComposeStatus


def compose(conn: Connection, user_id: UUID, local_date: date, *, now: datetime) -> ComposeResult:
    existing = _existing(conn, user_id, local_date)
    if existing is not None:
        return existing
    person = conn.execute(text("SELECT timezone, record_confirmed_at FROM users WHERE id = :u"), {"u": user_id}).one()
    asks = _new_asks(conn, user_id)
    matches = _still_open(conn, match.brief_matches(conn, user_id, local_date))
    since = _covers_since(conn, user_id, person.record_confirmed_at)
    activity = match.activity_since(conn, user_id, since) if since is not None else []
    waiting = profile.get_record(conn, user_id)["open_proposals"]
    status: ComposeStatus = "composed" if asks or matches or activity or waiting else "suppressed"

    in_app_id = _insert_brief(conn, user_id, local_date, "in_app", status, since)
    if in_app_id is None:  # a concurrent compose of this person and date committed first
        raced = _existing(conn, user_id, local_date)
        assert raced is not None, "the brief the other compose made must be visible now"
        return raced
    if asks:  # an ask comes ahead of the matches: it has a clock
        conn.execute(
            text(
                """INSERT INTO brief_items (brief_id, position, user_id, kind, introduction_id)
                   VALUES (:brief, :position, :u, 'introduction', :introduction)"""
            ),
            [
                {"brief": in_app_id, "position": n, "u": user_id, "introduction": ask.introduction_id}
                for n, ask in enumerate(asks, start=1)
            ],
        )
    if matches:
        conn.execute(
            text(
                """INSERT INTO brief_items (brief_id, position, user_id, kind, match_decision_id)
                   VALUES (:brief, :position, :u, 'match', :decision)"""
            ),
            [
                {"brief": in_app_id, "position": n, "u": user_id, "decision": m.decision_id}
                for n, m in enumerate(matches, start=len(asks) + 1)
            ],
        )
    email_id = outbox_id = None
    if status == "composed" and email_is_on(conn, user_id):
        release, expires = release_window(user_id, local_date, ZoneInfo(person.timezone), now)
        email_id = _insert_brief(conn, user_id, local_date, "email", "queued", since, send_after=release)
        if email_id is not None:
            outbox_id = outbox.add(
                conn,
                kind=EMAIL_TEMPLATE,
                dedupe_key=f"brief:{email_id}",
                user_id=user_id,
                payload={"brief_id": str(in_app_id)},  # ids only: the body is built when the email is sent
                not_before=release,
                expires_at=expires,
            )
    log.event(
        "brief.composed",
        brief_id=in_app_id,
        status=status,
        items=len(asks) + len(matches),
        has_email=outbox_id is not None,
    )
    return ComposeResult(in_app_id, email_id, outbox_id, status)


def email_is_on(conn: Connection, user_id: UUID) -> bool:
    """Whether the person has an active brief_email consent (ARCHITECTURE 8.1: checked by the brief and the relay)."""
    return any(c.purpose == "brief_email" and c.granted for c in trust.current_consents(conn, user_id))


def release_window(user_id: UUID, local_date: date, zone: ZoneInfo, now: datetime) -> tuple[datetime, datetime]:
    """When the email may go and when it stops being worth sending. The release is 06:30 plus a delay of 0 to 25 minutes
    that is the same every time for one person and date, and never earlier than `now`; the end is local noon."""
    delay = Random(f"{user_id}:{local_date}").randint(0, JITTER_MINUTES)
    release = datetime.combine(local_date, SEND_FROM, tzinfo=zone) + timedelta(minutes=delay)
    return max(release, now), datetime.combine(local_date, EXPIRES_AT, tzinfo=zone)


def _existing(conn: Connection, user_id: UUID, local_date: date) -> ComposeResult | None:
    row = conn.execute(
        text(
            """SELECT b.id, b.status::text AS status, e.id AS email_id, o.id AS outbox_id
               FROM briefs b
               LEFT JOIN briefs e ON e.user_id = b.user_id AND e.local_date = b.local_date AND e.channel = 'email'
               LEFT JOIN outbox o ON o.dedupe_key = 'brief:' || e.id::text
               WHERE b.user_id = :u AND b.local_date = :d AND b.channel = 'in_app'"""
        ),
        {"u": user_id, "d": local_date},
    ).one_or_none()
    if row is None:
        return None
    return ComposeResult(row.id, row.email_id, row.outbox_id, cast(ComposeStatus, row.status))


def _new_asks(conn: Connection, user_id: UUID) -> list[hiring.BriefAsk]:
    """The person's open asks that no earlier brief of theirs has listed (06-INDEX 10: each ask once per channel). Items
    are stored on the in-app brief and the email is written from it, so one ask is one item, in the first brief composed
    after it was asked and in no later one."""
    listed = set(
        conn.execute(
            text(
                """SELECT i.introduction_id FROM brief_items i JOIN briefs b ON b.id = i.brief_id
                    WHERE b.user_id = :u AND b.channel = 'in_app' AND i.kind = 'introduction'"""
            ),
            {"u": user_id},
        ).scalars()
    )
    return [ask for ask in hiring.asks_for_brief(conn, user_id) if ask.introduction_id not in listed]


def _still_open(conn: Connection, matches: list[match.BriefMatch]) -> list[match.BriefMatch]:
    """Drop a shown job whose apply link now fails (BRF-01). None, "could not tell", keeps it: the brief says so. A job
    nobody assessed has no apply link to vouch for here."""
    return [m for m in matches if not m.is_assessed or ingest.verify_live(conn, m.job_id) is not False]


def _covers_since(conn: Connection, user_id: UUID, record_confirmed_at: datetime | None) -> datetime | None:
    """Where "done since the last brief" starts: when the person's previous in-app brief was made, else when they
    confirmed their record. None only for a person with neither, who has nothing to be told about."""
    previous = conn.execute(
        text("SELECT max(created_at) FROM briefs WHERE user_id = :u AND channel = 'in_app'"), {"u": user_id}
    ).scalar()
    since: datetime | None = previous or record_confirmed_at
    return since


def _insert_brief(
    conn: Connection,
    user_id: UUID,
    local_date: date,
    channel: str,
    status: str,
    covers_since: datetime | None,
    *,
    send_after: datetime | None = None,
) -> UUID | None:
    """The new brief's id, or None when this person and date already have a brief of this channel."""
    new_id: UUID | None = conn.execute(
        text(
            """INSERT INTO briefs (user_id, local_date, channel, status, covers_since, send_after)
               VALUES (:u, :d, :channel, :status, :since, :send_after)
               ON CONFLICT ON CONSTRAINT briefs_one_per_day DO NOTHING
               RETURNING id"""
        ),
        {
            "u": user_id,
            "d": local_date,
            "channel": channel,
            "status": status,
            "since": covers_since,
            "send_after": send_after,
        },
    ).scalar()
    return new_id
