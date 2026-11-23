"""A candidate's view of an ask and their answer to it, and an employer's view of an accepted introduction (HIR-04).

The candidate opens an ask and sees exactly what would be shared (share_preview, fixed when the ask was made) with its
hash. Accepting is one transaction: the scoped consent, the status, the answer clock (the guard sets it), the shortlist
entry equal to the preview, a notice for the organization and an audit row. There is no way back. Declining is private.

Everything runs as the signed-in person under row-level security: a candidate reads their own rows, an employer member
reads only accepted rows of their organization, so nothing about an ask reaches an employer before a yes.
"""

import base64
import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.platform import events, outbox
from app.platform.errors import ApiError
from app.trust import api as trust

from . import access, errors, share

TERMS = (
    "If you accept, {organization} receives what is shown below at once and must answer yes or no with a reason "
    "within 5 business days. Accepting cannot be undone."
)  # the Twin sentence of API.md 3.12 waits for Phase 9 (index deviation 11)
SHARED = "{organization} can now see the items under 'on accept'. Your email is shared only if they answer yes."
ACCEPTED_TEMPLATE = "introduction_accepted_v1"

_CANDIDATE_COLUMNS = """i.id, i.status::text AS status, i.asked_at, i.expires_at, i.responded_at, i.answer_due_at,
       i.employer_answer::text AS employer_answer, i.employer_answer_reason, i.employer_answered_at,
       i.share_preview, i.role_id, i.organization_id, i.user_id"""


def encode_cursor(*parts: datetime | UUID) -> str:
    """An opaque cursor: the sort keys of the last row, as text."""
    raw = json.dumps([p.isoformat() if isinstance(p, datetime) else str(p) for p in parts])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        when, ident = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(when), UUID(ident)
    except (ValueError, TypeError):
        raise ApiError(
            400, "invalid_request", "That cursor was not issued by this list. Start the list again."
        ) from None


def pay_range(row: Any) -> dict[str, Any] | None:
    if row.salary_min is None and row.salary_max is None:
        return None
    return {"min": row.salary_min, "max": row.salary_max, "currency": row.salary_currency}


def employer_email(share_preview: dict[str, Any], answer: str | None) -> str | None:
    """The candidate's email, only once the employer has answered yes (the preview said so)."""
    if answer != "yes":
        return None
    email = (share_preview.get("on_employer_yes") or {}).get("email")
    return cast("str | None", email)


def _role_fields(conn: Connection, introduction_id: UUID) -> Any:
    return conn.execute(
        text("SELECT * FROM introduction_role(:id)"),
        {"id": introduction_id},
    ).one_or_none()


def _candidate_item(conn: Connection, row: Any) -> dict[str, Any]:
    role = _role_fields(conn, row.id)
    return {
        "id": row.id,
        "status": row.status,
        "asked_at": row.asked_at,
        "expires_at": row.expires_at,
        "responded_at": row.responded_at,
        "employer_answer": row.employer_answer,
        "employer_answered_at": row.employer_answered_at,
        "organization_name": None if role is None else role.organization_name,
        "role_title": None if role is None else role.role_title,
    }


def list_for_candidate(
    conn: Connection, user_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """The caller's own introductions of every status, newest ask first."""
    params: dict[str, Any] = {"user": user_id, "limit": limit + 1}
    after = ""
    if cursor is not None:
        params["c_at"], params["c_id"] = decode_cursor(cursor)
        after = "AND (i.asked_at, i.id) < (:c_at, :c_id)"
    rows = conn.execute(
        text(
            f"""SELECT {_CANDIDATE_COLUMNS} FROM introductions i
                WHERE i.user_id = :user {after}
                ORDER BY i.asked_at DESC, i.id DESC LIMIT :limit"""
        ),
        params,
    ).all()
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].asked_at, page[-1].id) if len(rows) > limit else None
    return [_candidate_item(conn, row) for row in page], next_cursor


def _employer_item(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "role": {"id": row.role_id, "title": row.role_title},
        "status": row.status,
        "responded_at": row.responded_at,
        "answer_due_at": row.answer_due_at,
        "is_overdue": row.is_overdue,
        "employer_answer": row.employer_answer,
        "employer_answer_reason": row.employer_answer_reason,
        "employer_answered_at": row.employer_answered_at,
        "shared": (row.share_preview or {}).get("on_accept"),
        "email": employer_email(row.share_preview or {}, row.employer_answer),
    }


_EMPLOYER_SELECT = f"""SELECT {_CANDIDATE_COLUMNS}, r.title AS role_title,
       (i.employer_answer IS NULL AND i.answer_due_at < now()) AS is_overdue
       FROM introductions i LEFT JOIN roles r ON r.id = i.role_id"""


def list_for_employer(
    conn: Connection,
    caller: UUID,
    *,
    role_id: UUID | None,
    org_id: UUID | None,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Accepted introductions of one organization (by role or by organization), newest answer first. The caller must be
    a member; the policy shows accepted rows only, so an asked candidate is not here."""
    if org_id is None:
        org_id = conn.execute(
            text("SELECT organization_id FROM roles WHERE id = :r"), {"r": role_id}
        ).scalar_one_or_none()
        if org_id is None:
            raise ApiError(404, "not_found", "No such resource.")
    access.require_member(conn, org_id)
    params: dict[str, Any] = {"org": org_id, "role": role_id, "status": status, "limit": limit + 1, "me": caller}
    after = ""
    if cursor is not None:
        params["c_at"], params["c_id"] = decode_cursor(cursor)
        after = "AND (i.responded_at, i.id) < (:c_at, :c_id)"
    rows = conn.execute(
        text(
            f"""{_EMPLOYER_SELECT}
                WHERE i.organization_id = :org AND i.user_id <> :me AND i.status = 'accepted'
                  AND (CAST(:role AS uuid) IS NULL OR i.role_id = :role)
                  AND (CAST(:status AS text) IS NULL OR i.status::text = :status) {after}
                ORDER BY i.responded_at DESC, i.id DESC LIMIT :limit"""
        ),
        params,
    ).all()
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].responded_at, page[-1].id) if len(rows) > limit else None
    return [_employer_item(row) for row in page], next_cursor


def get(conn: Connection, caller: UUID, introduction_id: UUID) -> dict[str, Any]:
    """One introduction: the candidate's own ask with its preview and hash, or an accepted one an employer member may
    see. Anything else is 404."""
    row = conn.execute(
        text(f"{_EMPLOYER_SELECT} WHERE i.id = :id"),
        {"id": introduction_id},
    ).one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "No such resource.")
    if row.user_id != caller:  # the policy admits only accepted rows of the caller's organizations
        return {"view": "employer", **_employer_item(row), "organization_id": row.organization_id}
    role = _role_fields(conn, row.id)
    if role is None:
        raise ApiError(404, "not_found", "No such resource.")
    has_signal = conn.execute(
        text(
            """SELECT EXISTS (SELECT 1 FROM interest_signals
                              WHERE user_id = :me AND role_id = :role AND sender_side = 'organization')"""
        ),
        {"me": caller, "role": row.role_id},
    ).scalar_one()
    return {
        "view": "candidate",
        "id": row.id,
        "status": row.status,
        "asked_at": row.asked_at,
        "expires_at": row.expires_at,
        "responded_at": row.responded_at,
        "employer_answer": row.employer_answer,
        "employer_answer_reason": row.employer_answer_reason,
        "employer_answered_at": row.employer_answered_at,
        "organization": {"id": row.organization_id, "name": role.organization_name, "is_synthetic": role.is_synthetic},
        "role": {
            "id": row.role_id,
            "title": role.role_title,
            "job_id": role.job_id,
            "country": role.country,
            "city": role.city,
            "work_arrangements": list(role.work_arrangements),
            "pay_range": pay_range(role),
        },
        "has_employer_signal": has_signal,
        "terms": TERMS.format(organization=role.organization_name),
        "share_preview": row.share_preview,
        "share_preview_hash": share.preview_hash(row.share_preview),
        "consent_text_version": trust.INTRODUCTION_TEXT_VERSION,
    }


def _own_ask_for_update(conn: Connection, user_id: UUID, introduction_id: UUID) -> Any:
    """The caller's ask, locked, when it can still be answered. Otherwise the error that says why (404 for someone
    else's, 409 for one that is resolved or has lapsed)."""
    columns = (
        "id, status::text AS status, share_preview, organization_id, role_id, expires_at, now() >= expires_at AS lapsed"
    )
    row = conn.execute(
        text(f"SELECT {columns} FROM introductions WHERE id = :id AND user_id = :me AND status = 'asked' FOR UPDATE"),
        {"id": introduction_id, "me": user_id},
    ).one_or_none()
    if row is None:  # no longer asked (or never visible): say which
        row = conn.execute(
            text(f"SELECT {columns} FROM introductions WHERE id = :id AND user_id = :me"),
            {"id": introduction_id, "me": user_id},
        ).one_or_none()
        if row is None:
            raise ApiError(404, "not_found", "No such resource.")
    if row.status in ("accepted", "declined"):
        raise ApiError(409, "already_resolved", "You have already answered this ask.", {"resolution": row.status})
    if row.status == "expired" or row.lapsed:
        raise ApiError(409, "conflict", "This ask has expired.", {"state": "expired"})
    if row.status != "asked":  # withdrawn: the role closed
        raise ApiError(409, "conflict", "This ask was withdrawn.", {"state": "closed"})
    return row


def _guard_refusal(exc: DBAPIError) -> ApiError | None:
    """What introductions_guard says when a move is refused, in the words of the API.md 1.11 catalogue."""
    known = errors.from_db(exc)
    if known is not None:
        return known
    message = getattr(getattr(getattr(exc, "orig", exc), "diag", None), "message_primary", None) or ""
    if message.startswith("introduction expired"):
        return ApiError(409, "conflict", "This ask has expired.", {"state": "expired"})
    if message.startswith("introduction cannot move"):
        return ApiError(409, "already_resolved", "You have already answered this ask.", {"resolution": "resolved"})
    return None


def accept(
    conn: Connection, user_id: UUID, introduction_id: UUID, share_preview_hash: str, consent_text_version: str
) -> dict[str, Any]:
    """Accept: everything in the caller's transaction, so a refusal anywhere leaves nothing shared."""
    row = _own_ask_for_update(conn, user_id, introduction_id)
    if share.preview_hash(row.share_preview) != share_preview_hash:  # before any write
        raise ApiError(409, "preview_changed", "What would be shared is not what you were shown. Open the ask again.")
    trust.grant_introduction_consent(conn, user_id, row.organization_id, row.role_id, consent_text_version)
    try:
        with conn.begin_nested():
            moved = conn.execute(
                text(
                    """UPDATE introductions SET status = 'accepted' WHERE id = :id
                       RETURNING id, status::text AS status, responded_at, answer_due_at"""
                ),
                {"id": introduction_id},
            ).one()
    except DBAPIError as exc:
        refusal = _guard_refusal(exc)
        if refusal is None:
            raise
        raise refusal from exc
    conn.execute(
        text(
            """INSERT INTO shortlist_entries
                 (organization_id, role_id, introduction_id, user_id, shared_payload, contact_released_at)
               VALUES (:org, :role, :id, :me, CAST(:payload AS jsonb), now())"""
        ),
        {
            "org": row.organization_id,
            "role": row.role_id,
            "id": introduction_id,
            "me": user_id,
            "payload": json.dumps(row.share_preview),
        },
    )
    outbox.add(
        conn,
        kind=ACCEPTED_TEMPLATE,
        dedupe_key=f"introduction_accepted:{introduction_id}",
        user_id=None,
        payload={"introduction_id": str(introduction_id)},
    )
    trust.audit_log(
        conn,
        "hiring.introduction_accepted",
        "candidate",
        user_id,
        {"introduction_id": introduction_id, "role_id": row.role_id},
        entity_table="introductions",
        entity_id=str(introduction_id),
        organization_id=row.organization_id,
    )
    events.publish(
        conn,
        "introduction.accepted",
        {"introduction_id": str(introduction_id), "user_id": str(user_id)},
        key=str(introduction_id),
    )
    organization = _role_fields(conn, introduction_id).organization_name
    return {
        "introduction": {
            "id": moved.id,
            "status": moved.status,
            "responded_at": moved.responded_at,
            "answer_due_at": moved.answer_due_at,
        },
        "shared": SHARED.format(organization=organization),
    }


def decline(conn: Connection, user_id: UUID, introduction_id: UUID) -> dict[str, Any]:
    """Decline privately: no reason is stored and the employer never learns of it (index deviation 14)."""
    row = _own_ask_for_update(conn, user_id, introduction_id)
    try:
        with conn.begin_nested():
            moved = conn.execute(
                text(
                    """UPDATE introductions SET status = 'declined' WHERE id = :id
                       RETURNING id, status::text AS status, responded_at"""
                ),
                {"id": introduction_id},
            ).one()
    except DBAPIError as exc:
        refusal = _guard_refusal(exc)
        if refusal is None:
            raise
        raise refusal from exc
    trust.audit_log(
        conn,
        "hiring.introduction_declined",
        "candidate",
        user_id,
        {"introduction_id": introduction_id, "role_id": row.role_id},
        entity_table="introductions",
        entity_id=str(introduction_id),
        organization_id=row.organization_id,
    )
    return {"introduction": {"id": moved.id, "status": moved.status, "responded_at": moved.responded_at}}
