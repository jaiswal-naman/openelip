"""The application tracker (APP-01): what a person applied to and what happened next, with the time of each step.

A person has one application per job (DATA_MODEL 3.8). Every status change is an event in an append-only history, and
the application row keeps the latest status for fast reads. The times are the candidate's own account of their own
application, so the rules only stop impossible orderings (a status that predates the current one) and dates in the
future. Nothing here sends, fetches or approves anything: the tracker records what the person tells it.

Job titles and company names come through ingest.api (ARCHITECTURE 3.3). Every query names the person as well as
relying on row-level security; an application that is not the caller's is the same 404 as one that does not exist.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, Row, text

from app.ingest import api as ingest
from app.platform.errors import ApiError

from .models import ApplicationStatus

OCCURRED_AT_SKEW = timedelta(minutes=5)  # assumption: a person's own clock may run a little ahead of the server's
# ponytail: no paging. A tracker holds tens of rows; past this the newest are listed. A cursor (API.md 1.4) is the
# upgrade when anyone gets there.
MAX_LISTED = 200

APPLICATIONS = text(
    """SELECT id, job_id, application_packet_id, status::text AS status, status_changed_at
         FROM applications WHERE user_id = :u ORDER BY status_changed_at DESC, created_at DESC, id LIMIT :limit"""
)
ONE_APPLICATION = text(
    """SELECT id, job_id, application_packet_id, status::text AS status, status_changed_at
         FROM applications WHERE user_id = :u AND id = :id"""
)
# The lock is on the caller's own row only; a second status for the same application waits for the first.
LOCK_APPLICATION = text(
    """SELECT status::text AS status, status_changed_at FROM applications WHERE user_id = :u AND id = :id FOR UPDATE"""
)
EVENTS = text(
    """SELECT application_id, status::text AS status, occurred_at, created_at
         FROM application_events WHERE user_id = :u AND application_id = ANY(CAST(:ids AS uuid[]))
        ORDER BY application_id, occurred_at, id"""
)
PACKET_OF_JOB = text("SELECT 1 FROM application_packets WHERE user_id = :u AND id = :p AND job_id = :j")
# DO NOTHING on the (user_id, job_id) key: a second application for the same job gets no row back, and the caller
# answers 409.
INSERT_APPLICATION = text(
    """INSERT INTO applications (user_id, job_id, application_packet_id, status, status_changed_at)
       VALUES (:u, :j, :p, 'applied', :at) ON CONFLICT (user_id, job_id) DO NOTHING RETURNING id"""
)
INSERT_EVENT = text(
    """INSERT INTO application_events (application_id, user_id, status, occurred_at) VALUES (:a, :u, :s, :at)"""
)
MOVE_STATUS = text("""UPDATE applications SET status = :s, status_changed_at = :at WHERE user_id = :u AND id = :id""")


class EventView(BaseModel):
    status: ApplicationStatus
    occurred_at: datetime  # when the candidate says it happened
    created_at: datetime  # when it was recorded


class ApplicationView(BaseModel):
    """One application as the tracker shows it, and as the chat tool reads it."""

    id: UUID
    job_id: int
    job_title: str
    company_name: str
    status: ApplicationStatus  # the latest event's
    status_changed_at: datetime
    application_packet_id: UUID | None
    events: list[EventView]  # oldest first, by the time the candidate gave


def not_found() -> ApiError:
    """The answer for anything that is not the caller's, or does not exist: the same, so ids reveal nothing."""
    return ApiError(404, "not_found", "No such resource.")


def _invalid_time(issue: str) -> ApiError:
    return ApiError(
        422,
        "validation_failed",
        "One or more fields are invalid.",
        {"fields": [{"field": "occurred_at", "issue": issue}]},
    )


def _stated_time(occurred_at: datetime | None, *, not_before: datetime | None) -> datetime:
    """The time the person says it happened (now when they say nothing), or a 422 for one that cannot be true."""
    now = datetime.now(timezone.utc)
    when = occurred_at or now
    if when > now + OCCURRED_AT_SKEW:
        raise _invalid_time("cannot be in the future")
    if not_before is not None and when < not_before:
        raise _invalid_time("cannot be earlier than the current status")
    return when


def _views(conn: Connection, user_id: UUID, rows: list[Row[Any]]) -> list[ApplicationView]:
    if not rows:
        return []
    events: dict[UUID, list[EventView]] = defaultdict(list)
    for event in conn.execute(EVENTS, {"u": user_id, "ids": [row.id for row in rows]}):
        events[event.application_id].append(
            EventView(status=event.status, occurred_at=event.occurred_at, created_at=event.created_at)
        )
    # One read per distinct job. A job the index no longer lists cannot happen (nothing unlists one), but the person's
    # history is kept rather than dropped if it ever does: the application then shows no title.
    jobs = {job_id: ingest.get_job(conn, job_id) or {} for job_id in {row.job_id for row in rows}}
    return [
        ApplicationView(
            id=row.id,
            job_id=row.job_id,
            job_title=jobs[row.job_id].get("title", ""),
            company_name=jobs[row.job_id].get("company_name", ""),
            status=row.status,
            status_changed_at=row.status_changed_at,
            application_packet_id=row.application_packet_id,
            events=events[row.id],
        )
        for row in rows
    ]


def list_applications(conn: Connection, user_id: UUID) -> list[ApplicationView]:
    """The person's applications, the most recently changed first, each with its whole history."""
    return _views(conn, user_id, list(conn.execute(APPLICATIONS, {"u": user_id, "limit": MAX_LISTED})))


def _one(conn: Connection, user_id: UUID, application_id: UUID) -> ApplicationView:
    return _views(conn, user_id, [conn.execute(ONE_APPLICATION, {"u": user_id, "id": application_id}).one()])[0]


def create_application(
    conn: Connection, user_id: UUID, job_id: int, packet_id: UUID | None, occurred_at: datetime | None
) -> ApplicationView:
    """Record that the person applied: the application and its first event, in the caller's transaction."""
    if ingest.get_job(conn, job_id) is None:
        raise not_found()
    when = _stated_time(occurred_at, not_before=None)
    if (
        packet_id is not None
        and conn.execute(PACKET_OF_JOB, {"u": user_id, "p": packet_id, "j": job_id}).first() is None
    ):
        raise not_found()  # another person's packet, another job's packet and a packet that is not there look the same
    created = conn.execute(
        INSERT_APPLICATION, {"u": user_id, "j": job_id, "p": packet_id, "at": when}
    ).scalar_one_or_none()
    if created is None:
        raise ApiError(
            409, "conflict", "You already track an application for this job.", {"state": "application_exists"}
        )
    conn.execute(INSERT_EVENT, {"a": created, "u": user_id, "s": "applied", "at": when})
    return _one(conn, user_id, created)


def record_status(
    conn: Connection, user_id: UUID, application_id: UUID, status: ApplicationStatus, occurred_at: datetime
) -> ApplicationView:
    """Record a new status with the time the person says it happened. It moves the application's status and time."""
    current = conn.execute(LOCK_APPLICATION, {"u": user_id, "id": application_id}).one_or_none()
    if current is None:
        raise not_found()
    if status == current.status:
        raise ApiError(409, "conflict", "The application already has this status.", {"state": "same_status"})
    when = _stated_time(occurred_at, not_before=current.status_changed_at)
    conn.execute(INSERT_EVENT, {"a": application_id, "u": user_id, "s": status, "at": when})
    conn.execute(MOVE_STATUS, {"u": user_id, "id": application_id, "s": status, "at": when})
    return _one(conn, user_id, application_id)
