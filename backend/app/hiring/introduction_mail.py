"""The introduction emails, templates introduction_accepted_v1 (to the organization) and introduction_answered_v1
(to the candidate) (HIR-04, HIR-06). Registered with the relay when this module is imported; app.hiring.jobs imports it,
so the worker loads it.

The outbox row holds the introduction id only. The recipient, the wording and the link are worked out at send time. The
organization's notice names the role and the due time, never the candidate (index deviation 13).
"""

from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text

from app.platform import config, relay
from app.platform.relay import Outgoing, Refusal


def _line(value: str) -> str:
    """One line of plain text: titles and reasons are typed by other people, so no line break survives."""
    return " ".join(value.split())


def _introduction_id(payload: dict[str, Any]) -> UUID | None:
    try:
        return UUID(str(payload["introduction_id"]))
    except (KeyError, TypeError, ValueError):
        return None


def _notice_recipient(conn: Connection, organization_id: UUID, creator: UUID | None) -> str | None:
    """The role's creator while they are still an active member, else the earliest-joined active owner."""
    email = conn.execute(
        text(
            """SELECT u.email FROM org_members m JOIN users u ON u.id = m.user_id
               WHERE m.organization_id = :org AND m.user_id = :creator AND m.status = 'active'"""
        ),
        {"org": organization_id, "creator": creator},
    ).scalar_one_or_none()
    if email is None:
        email = conn.execute(
            text(
                """SELECT u.email FROM org_members m JOIN users u ON u.id = m.user_id
                   WHERE m.organization_id = :org AND m.status = 'active' AND m.role = 'owner'
                   ORDER BY m.joined_at, m.id LIMIT 1"""
            ),
            {"org": organization_id},
        ).scalar_one_or_none()
    return None if email is None else str(email)


def resolve_accepted(conn: Connection, payload: dict[str, Any]) -> Outgoing | Refusal:
    """introduction_accepted_v1: tell the organization that someone said yes, and when its answer is due."""
    introduction_id = _introduction_id(payload)
    if introduction_id is None:
        return Refusal("failed", "bad_payload")
    row = conn.execute(
        text(
            """SELECT i.status::text AS status, i.answer_due_at, i.role_id, i.organization_id, r.title, r.created_by,
                      o.timezone
               FROM introductions i JOIN roles r ON r.id = i.role_id JOIN organizations o ON o.id = i.organization_id
               WHERE i.id = :id"""
        ),
        {"id": introduction_id},
    ).one_or_none()
    if row is None or row.status != "accepted" or row.answer_due_at is None:
        return Refusal("cancelled", "stale")
    to = _notice_recipient(conn, row.organization_id, row.created_by)
    if to is None:
        return Refusal("cancelled", "no_recipient")
    settings = config.get()
    due: datetime = row.answer_due_at.astimezone(ZoneInfo(row.timezone))
    link = f"{settings.app_base_url.rstrip('/')}/hire/shortlist/{row.role_id}"
    title = _line(row.title)
    body = (
        f"A candidate said yes to your role {title}.\n\n"
        f"You owe them an answer, yes or no with a reason, by {due:%A %d %B %Y, %H:%M} ({row.timezone}).\n"
        f"Open the shortlist: {link}\n\n"
        f"Sent by {settings.product_name}.\n"
    )
    return Outgoing(to, f"A candidate said yes to {title}", body)


def resolve_answered(conn: Connection, payload: dict[str, Any]) -> Outgoing | Refusal:
    """introduction_answered_v1: tell the candidate what the organization answered, and why."""
    introduction_id = _introduction_id(payload)
    if introduction_id is None:
        return Refusal("failed", "bad_payload")
    row = conn.execute(
        text(
            """SELECT i.employer_answer::text AS answer, i.employer_answer_reason AS reason, r.title, o.name, u.email
               FROM introductions i JOIN roles r ON r.id = i.role_id JOIN organizations o ON o.id = i.organization_id
               JOIN users u ON u.id = i.user_id
               WHERE i.id = :id"""
        ),
        {"id": introduction_id},
    ).one_or_none()
    if row is None or row.answer is None:
        return Refusal("cancelled", "stale")
    settings = config.get()
    organization, title = _line(row.name), _line(row.title)
    body = (
        f"{organization} answered {row.answer} for {title}: {_line(row.reason)}\n\n"
        f"See it in the app: {settings.app_base_url.rstrip('/')}/app/today?view=decisions\n\n"
        f"Sent by {settings.product_name}.\n"
    )
    return Outgoing(row.email, f"{organization} answered {row.answer} for {title}", body)


relay.RESOLVERS["introduction_accepted_v1"] = resolve_accepted
relay.RESOLVERS["introduction_answered_v1"] = resolve_answered
