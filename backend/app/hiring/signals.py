"""Serious-interest signals (HIR-08): scarce, and seen by the other side only as the rules allow.

Each side may send SIGNALS_PER_WEEK a UTC week, and one per role. A candidate may signal on an ask or an accepted
introduction; the employer sees that signal only after acceptance (the policy). An organization may signal only on an
introduction the candidate has accepted (owner decision G7; the database refuses anything else too). The weekly limit is
a unique key on (side, week, slot), so two sends racing for the last slot cannot both succeed.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.platform.errors import ApiError
from app.trust import api as trust

from . import access

SIGNALS_PER_WEEK = 3  # assumption, also the database's slot CHECK
WEEK = "date_trunc('week', now() AT TIME ZONE 'UTC')::date"
RESETS_AT = "(date_trunc('week', now() AT TIME ZONE 'UTC') + interval '7 days') AT TIME ZONE 'UTC'"


def _quota(conn: Connection, side: str, candidate: UUID, organization: UUID) -> dict[str, Any]:
    who = "user_id = :who" if side == "candidate" else "organization_id = :who"
    row = conn.execute(
        text(
            f"""SELECT count(*) AS used, {RESETS_AT} AS resets_at FROM interest_signals
                WHERE sender_side = CAST(:side AS signal_side) AND {who} AND week_start = {WEEK}"""
        ),
        {"side": side, "who": candidate if side == "candidate" else organization},
    ).one()
    return {"used": row.used, "limit": SIGNALS_PER_WEEK, "resets_at": row.resets_at}


def _exhausted(quota: dict[str, Any]) -> ApiError:
    return ApiError(
        422,
        "quota_exhausted",
        "You have sent all your serious-interest signals for this week.",
        {"resets_at": quota["resets_at"].isoformat()},
    )


def _constraint(exc: IntegrityError) -> str:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None) or ""


def send(conn: Connection, caller: UUID, introduction_id: UUID) -> dict[str, Any]:
    """Send one signal on an introduction the caller can see. The side is the caller's: the candidate of the
    introduction, else a member of the organization (who can see it only once it is accepted)."""
    row = conn.execute(
        text(
            """SELECT id, user_id, organization_id, role_id, status::text AS status, now() >= expires_at AS lapsed
               FROM introductions WHERE id = :id"""
        ),
        {"id": introduction_id},
    ).one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "No such resource.")
    side = "candidate" if row.user_id == caller else "organization"
    if side == "organization":
        access.require_member(conn, row.organization_id)
    state = "expired" if row.status == "asked" and row.lapsed else row.status
    if state not in ("asked", "accepted"):
        raise ApiError(
            409, "conflict", "This introduction is closed.", {"state": "closed" if state == "withdrawn" else state}
        )
    already = conn.execute(
        text(
            """SELECT EXISTS (SELECT 1 FROM interest_signals
                              WHERE sender_side = CAST(:side AS signal_side) AND user_id = :cand AND role_id = :role)"""
        ),
        {"side": side, "cand": row.user_id, "role": row.role_id},
    ).scalar_one()
    if already:
        raise ApiError(409, "already_resolved", "You have already sent a signal for this role.", {"resolution": "sent"})
    inserted = None
    for _ in range(2):  # a second pass only after losing a race for the same slot
        quota = _quota(conn, side, row.user_id, row.organization_id)
        if quota["used"] >= SIGNALS_PER_WEEK:
            raise _exhausted(quota)
        try:
            with conn.begin_nested():
                inserted = conn.execute(
                    text(
                        f"""INSERT INTO interest_signals
                              (sender_side, user_id, organization_id, role_id, introduction_id, sent_by,
                               week_start, slot)
                            VALUES (CAST(:side AS signal_side), :cand, :org, :role, :intro, :me, {WEEK}, :slot)
                            RETURNING id, created_at"""
                    ),
                    {
                        "side": side,
                        "cand": row.user_id,
                        "org": row.organization_id,
                        "role": row.role_id,
                        "intro": introduction_id if side == "organization" else None,
                        "me": caller,
                        "slot": quota["used"] + 1,
                    },
                ).one()
            break
        except IntegrityError as exc:
            if _constraint(exc) == "interest_signals_once":
                raise ApiError(
                    409, "already_resolved", "You have already sent a signal for this role.", {"resolution": "sent"}
                ) from exc
            if not _constraint(exc).endswith("_week"):
                raise
    if inserted is None:
        raise _exhausted(_quota(conn, side, row.user_id, row.organization_id))
    trust.audit_log(
        conn,
        "hiring.signal_sent",
        "candidate" if side == "candidate" else "employer",
        caller if side == "candidate" else None,
        {"introduction_id": introduction_id, "role_id": row.role_id, "side": side},
        actor_user_id=caller,
        entity_table="interest_signals",
        entity_id=str(inserted.id),
        organization_id=row.organization_id,
    )
    return {
        "signal": {
            "id": inserted.id,
            "sender_side": side,
            "role_id": row.role_id,
            "created_at": inserted.created_at,
            "is_visible_to_employer": side == "organization" or row.status == "accepted",
        },
        "quota": _quota(conn, side, row.user_id, row.organization_id),
    }


def _signal_item(row: Any) -> dict[str, Any]:
    created: datetime = row.created_at
    return {
        "id": row.id,
        "sender_side": row.sender_side,
        "role_id": row.role_id,
        "introduction_id": row.introduction_id,
        "created_at": created,
    }


_COLUMNS = """x.id, x.sender_side::text AS sender_side, x.role_id, x.created_at,
       COALESCE(x.introduction_id, (SELECT i.id FROM introductions i WHERE i.user_id = x.user_id
                                    AND i.role_id = x.role_id
                                    AND i.organization_id = x.organization_id)) AS introduction_id"""


def list_with_quota(conn: Connection, caller: UUID, org_id: UUID | None) -> dict[str, Any]:
    """Signals the caller sent and received, with the remaining weekly quota. For a candidate (no org_id) every signal
    about them; for an organization member the organization's signals and the candidate signals the policy shows
    (accepted introductions only)."""
    if org_id is None:
        rows = conn.execute(
            text(
                f"""SELECT {_COLUMNS}, x.sender_side::text = 'candidate' AS is_sent FROM interest_signals x
                    WHERE x.user_id = :me ORDER BY x.created_at DESC, x.id DESC"""
            ),
            {"me": caller},
        ).all()
        quota = _quota(conn, "candidate", caller, caller)
    else:
        access.require_member(conn, org_id)
        rows = conn.execute(
            text(
                f"""SELECT {_COLUMNS}, x.sender_side::text = 'organization' AS is_sent FROM interest_signals x
                    WHERE x.organization_id = :org AND x.user_id <> :me ORDER BY x.created_at DESC, x.id DESC"""
            ),
            {"org": org_id, "me": caller},
        ).all()
        quota = _quota(conn, "organization", caller, org_id)
    return {
        "sent": [_signal_item(r) for r in rows if r.is_sent],
        "received": [_signal_item(r) for r in rows if not r.is_sent],
        "quota": quota,
    }
