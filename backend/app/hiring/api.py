"""What other modules may call from app.hiring (the shared contracts in the Phase 6 index). Nothing else in this
package is a public interface.

  my_organizations(conn, user_id)         the organizations a person belongs to and the invitations waiting for them
  asks_for_brief(conn, user_id)           the person's open asks, newest first (the worker side: the brief's compose)
  brief_asks(conn, introduction_ids)      organization, role and expiry of the caller's own introductions

Membership, Invitation and BriefAsk are what they return.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from .queries import Invitation, Membership, my_organizations

__all__ = ["BriefAsk", "Invitation", "Membership", "asks_for_brief", "brief_asks", "my_organizations"]


@dataclass(frozen=True)
class BriefAsk:
    """One ask as the brief names it: who asks, about which role, and until when the candidate may answer."""

    introduction_id: UUID
    organization_name: str
    role_title: str
    expires_at: datetime


def asks_for_brief(conn: Connection, user_id: UUID) -> list[BriefAsk]:
    """The person's asked introductions that have not expired, newest first. For the compose job, which runs on a worker
    connection; it reads hiring's own tables, so the candidate's row-level security is not involved."""
    rows = conn.execute(
        text(
            """SELECT i.id, o.name AS organization_name, r.title AS role_title, i.expires_at
                 FROM introductions i
                 JOIN roles r ON r.id = i.role_id
                 JOIN organizations o ON o.id = i.organization_id
                WHERE i.user_id = :u AND i.status = 'asked' AND i.expires_at > now()
                ORDER BY i.asked_at DESC, i.id"""
        ),
        {"u": user_id},
    )
    return [BriefAsk(r.id, r.organization_name, r.role_title, r.expires_at) for r in rows]


def brief_asks(conn: Connection, introduction_ids: Sequence[UUID]) -> dict[UUID, BriefAsk]:
    """Organization, role and expiry of the caller's own introductions, of any status, by id. It reads through
    introduction_role(), which answers for the caller only (a candidate has no access to roles or organizations), so
    `app.user_id` must be the brief's owner: it is for the candidate's own request, and the brief module sets it for the
    email. Another person's id, or none at all, gives nothing."""
    if not introduction_ids:
        return {}
    rows = conn.execute(
        text(
            """SELECT t.id, ir.organization_name, ir.role_title, i.expires_at
                 FROM unnest(CAST(:ids AS uuid[])) AS t(id)
                 JOIN introductions i ON i.id = t.id AND i.user_id = app_user_id()
                CROSS JOIN LATERAL introduction_role(t.id) ir"""
        ),
        {"ids": list(introduction_ids)},
    )
    return {r.id: BriefAsk(r.id, r.organization_name, r.role_title, r.expires_at) for r in rows}
