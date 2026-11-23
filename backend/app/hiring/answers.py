"""The employer's side of an accepted introduction: the said-yes shortlist, the answer (yes or no, always with a reason)
and the answer clock (HIR-05, HIR-06).

All reads and writes run as the signed-in member under row-level security. The employer policies admit accepted rows
only, so the shortlist and the clock never mention a candidate who has not said yes. The candidate's email comes from
the frozen share_preview and is returned only once the employer has answered yes.
"""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.platform import outbox
from app.platform.errors import ApiError
from app.trust import api as trust

from . import access, errors
from .introductions import decode_cursor, employer_email, encode_cursor

MAX_REASON = 1000  # assumption: characters of an answer's reason
ANSWERED_TEMPLATE = "introduction_answered_v1"
SATURDAY = 5  # date.weekday(): Monday is 0


def business_days_between(now: datetime, due: datetime, tz: str) -> int:
    """Whole Monday-to-Friday days after today's date in the organization's time zone, up to and including the due date.

    0 when the answer is overdue. A due time on a Monday, seen on the Friday before, is 1. No holidays.
    """
    if due <= now:
        return 0
    zone = ZoneInfo(tz)
    today, last = now.astimezone(zone).date(), due.astimezone(zone).date()
    days = 0
    day: date = today + timedelta(days=1)
    while day <= last:
        if day.weekday() < SATURDAY:
            days += 1
        day += timedelta(days=1)
    return days


def answer(conn: Connection, caller: UUID, introduction_id: UUID, verdict: str, reason: str) -> dict[str, Any]:
    """The employer answers once. A repeat is 409 already_resolved; an introduction the caller cannot see is 404."""
    seen = conn.execute(
        text(
            """SELECT i.id, i.user_id, i.organization_id, i.role_id, i.share_preview,
                      i.employer_answer::text AS employer_answer
               FROM introductions i WHERE i.id = :id"""
        ),
        {"id": introduction_id},
    ).one_or_none()
    if seen is None or seen.user_id == caller:  # not visible, or the caller's own ask (a candidate cannot answer)
        raise ApiError(404, "not_found", "No such resource.")
    access.require_member(conn, seen.organization_id)
    resolved = ApiError(
        409,
        "already_resolved",
        "This introduction has already been answered.",
        {"resolution": seen.employer_answer},
    )
    if seen.employer_answer is not None:
        raise resolved
    try:
        with conn.begin_nested():
            moved = conn.execute(
                text(
                    """UPDATE introductions
                       SET employer_answer = CAST(:answer AS employer_answer), employer_answer_reason = :reason,
                           employer_answered_by = :me
                       WHERE id = :id AND employer_answer IS NULL
                       RETURNING employer_answer::text AS employer_answer, employer_answer_reason,
                                 employer_answered_at, answer_due_at, status::text AS status"""
                ),
                {"answer": verdict, "reason": reason, "me": caller, "id": introduction_id},
            ).one_or_none()
    except DBAPIError as exc:
        refusal = errors.from_db(exc)
        if refusal is not None:
            raise refusal from exc
        message = getattr(getattr(exc.orig, "diag", None), "message_primary", None) or ""
        if message.startswith("the employer answers once"):
            raise resolved from exc
        raise
    if moved is None:  # a second answer won the race: the policy no longer matches the row
        raise resolved
    outbox.add(
        conn,
        kind=ANSWERED_TEMPLATE,
        dedupe_key=f"introduction_answered:{introduction_id}",
        user_id=seen.user_id,
        payload={"introduction_id": str(introduction_id)},
    )
    # a row about the candidate has no subject: the audit policy admits only a row about the caller or about no one
    trust.audit_log(
        conn,
        "hiring.introduction_answered",
        "employer",
        None,
        {"introduction_id": introduction_id, "role_id": seen.role_id, "answer": verdict},
        actor_user_id=caller,
        entity_table="introductions",
        entity_id=str(introduction_id),
        organization_id=seen.organization_id,
    )
    organization = conn.execute(
        text("SELECT name FROM organizations WHERE id = :o"), {"o": seen.organization_id}
    ).scalar_one()
    return {
        "introduction": {
            "id": introduction_id,
            "status": moved.status,
            "employer_answer": moved.employer_answer,
            "employer_answer_reason": moved.employer_answer_reason,
            "employer_answered_at": moved.employer_answered_at,
            "is_on_time": moved.employer_answered_at <= moved.answer_due_at,
        },
        "candidate_sees": f"{organization} said {moved.employer_answer}: {moved.employer_answer_reason}",
        "email": employer_email(seen.share_preview, moved.employer_answer),
    }


def shortlist(
    conn: Connection, caller: UUID, role_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """Candidates who said yes to this role, newest first, each with exactly what they shared."""
    org_id = conn.execute(text("SELECT organization_id FROM roles WHERE id = :r"), {"r": role_id}).scalar_one_or_none()
    if org_id is None:
        raise ApiError(404, "not_found", "No such resource.")
    access.require_member(conn, org_id)
    params: dict[str, Any] = {"role": role_id, "org": org_id, "me": caller, "limit": limit + 1}
    after = ""
    if cursor is not None:
        params["c_at"], params["c_id"] = decode_cursor(cursor)
        after = "AND (s.created_at, s.id) < (:c_at, :c_id)"
    rows = conn.execute(
        text(
            f"""SELECT s.id AS entry_id, s.created_at AS entry_at, s.shared_payload, i.id AS introduction_id,
                       i.status::text AS status, i.responded_at, i.answer_due_at,
                       (i.employer_answer IS NULL AND i.answer_due_at < now()) AS is_overdue,
                       i.employer_answer::text AS employer_answer, i.employer_answer_reason, i.employer_answered_at,
                       i.share_preview,
                       EXISTS (SELECT 1 FROM interest_signals x WHERE x.sender_side = 'candidate'
                               AND x.user_id = s.user_id AND x.role_id = s.role_id) AS from_candidate,
                       EXISTS (SELECT 1 FROM interest_signals x WHERE x.sender_side = 'organization'
                               AND x.user_id = s.user_id AND x.role_id = s.role_id) AS from_you
                FROM shortlist_entries s JOIN introductions i ON i.id = s.introduction_id
                WHERE s.role_id = :role AND s.organization_id = :org AND s.user_id <> :me
                  AND i.status = 'accepted' {after}
                ORDER BY s.created_at DESC, s.id DESC LIMIT :limit"""
        ),
        params,
    ).all()
    page = rows[:limit]
    items = [
        {
            "introduction_id": row.introduction_id,
            "status": row.status,
            "responded_at": row.responded_at,
            "answer_due_at": row.answer_due_at,
            "is_overdue": row.is_overdue,
            "employer_answer": row.employer_answer,
            "employer_answer_reason": row.employer_answer_reason,
            "employer_answered_at": row.employer_answered_at,
            "shared": row.shared_payload.get("on_accept"),
            "signals": {"from_candidate": row.from_candidate, "from_you": row.from_you},
            "email": employer_email(row.share_preview, row.employer_answer),
        }
        for row in page
    ]
    next_cursor = encode_cursor(page[-1].entry_at, page[-1].entry_id) if len(rows) > limit else None
    return items, next_cursor


def answer_clock(conn: Connection, caller: UUID, org_id: UUID) -> dict[str, Any]:
    """What the organization owes, due times, whether new asks are paused, and its record of the last 30 days
    (DATA_MODEL 6.4). Read from stored introductions; measured, not estimated."""
    access.require_member(conn, org_id)
    org = conn.execute(
        text(
            """SELECT timezone, now() AS as_of, organization_has_overdue_answers(id) AS paused
               FROM organizations WHERE id = :o"""
        ),
        {"o": org_id},
    ).one()
    owed_rows = conn.execute(
        text(
            """SELECT i.id, i.role_id, r.title, s.shared_payload #>> '{on_accept,name}' AS name, i.responded_at,
                      i.answer_due_at, i.answer_due_at < now() AS is_overdue
               FROM introductions i JOIN roles r ON r.id = i.role_id
               LEFT JOIN shortlist_entries s ON s.introduction_id = i.id
               WHERE i.organization_id = :o AND i.status = 'accepted' AND i.employer_answer IS NULL
                 AND i.user_id <> :me
               ORDER BY i.answer_due_at, i.id"""
        ),
        {"o": org_id, "me": caller},
    ).all()
    overdue = [row for row in owed_rows if row.is_overdue]
    record = conn.execute(
        text(
            """SELECT count(*) AS total, count(*) FILTER (WHERE employer_answered_at <= answer_due_at) AS on_time
               FROM introductions
               WHERE organization_id = :o AND status = 'accepted' AND employer_answer IS NOT NULL
                 AND employer_answered_at >= now() - interval '30 days'"""
        ),
        {"o": org_id},
    ).one()
    count = len(overdue)
    return {
        "org_id": org_id,
        "timezone": org.timezone,
        "as_of": org.as_of,
        "is_asking_paused": org.paused,
        "paused_since": min((row.answer_due_at for row in overdue), default=None),
        "pause_reason": (
            f"{count} {'answer is' if count == 1 else 'answers are'} overdue. "
            "New introductions resume for every role when "
            f"{'it is' if count == 1 else 'they are'} answered."
            if org.paused
            else None
        ),
        "owed": [
            {
                "introduction_id": row.id,
                "role": {"id": row.role_id, "title": row.title},
                "name": row.name,
                "responded_at": row.responded_at,
                "answer_due_at": row.answer_due_at,
                "business_days_left": business_days_between(org.as_of, row.answer_due_at, org.timezone),
                "is_overdue": row.is_overdue,
            }
            for row in owed_rows
        ],
        "answered_last_30_days": {"total": record.total, "on_time": record.on_time},
    }
