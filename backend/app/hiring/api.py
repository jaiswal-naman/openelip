"""What other modules may call from app.hiring (the shared contracts in the Phase 6 index). Nothing else in this
package is a public interface.

  my_organizations(conn, user_id)         the organizations a person belongs to and the invitations waiting for them
  asks_for_brief(conn, user_id)           the person's open asks, newest first (the worker side: the brief's compose)
  brief_asks(conn, introduction_ids)      organization, role and expiry of the caller's own introductions
  introduction_ref(conn, introduction_id) who an introduction is between and where it stands, for campaigns (Phase 7)
  introduction_labels(conn, ids)          organization and role names of the introductions the caller can see (Phase 9)

Membership, Invitation and BriefAsk are what they return.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from .queries import Invitation, Membership, my_organizations

__all__ = [
    "BriefAsk",
    "IntroductionLabel",
    "IntroductionRef",
    "Invitation",
    "Membership",
    "asks_for_brief",
    "brief_asks",
    "introduction_labels",
    "introduction_ref",
    "my_organizations",
]


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


@dataclass(frozen=True)
class IntroductionRef:
    """One introduction as a campaign needs it: the candidate, the employer, the role, its job and where it stands."""

    user_id: UUID
    organization_id: UUID
    role_id: UUID
    job_id: int | None  # the posting the role is published as; None only for a role that is not open
    status: str  # asked, accepted, declined, expired or withdrawn
    employer_answer: str | None  # yes or no once the employer answered an accepted introduction
    answer_due_at: datetime | None


def introduction_ref(conn: Connection, introduction_id: UUID) -> IntroductionRef | None:
    """The introduction, or None when this connection cannot see it. The job id comes from introduction_role() when the
    transaction acts for the candidate (the API, and the worker with the person's id set), and from the role itself on
    a worker connection that sees every row."""
    row = conn.execute(
        text(
            """SELECT i.user_id, i.organization_id, i.role_id, i.status::text AS status,
                      i.employer_answer::text AS employer_answer, i.answer_due_at,
                      COALESCE((SELECT ir.job_id FROM introduction_role(i.id) ir),
                               (SELECT r.job_id FROM roles r WHERE r.id = i.role_id)) AS job_id
                 FROM introductions i WHERE i.id = :i"""
        ),
        {"i": introduction_id},
    ).one_or_none()
    if row is None:
        return None
    return IntroductionRef(
        user_id=row.user_id,
        organization_id=row.organization_id,
        role_id=row.role_id,
        job_id=row.job_id,
        status=row.status,
        employer_answer=row.employer_answer,
        answer_due_at=row.answer_due_at,
    )


@dataclass(frozen=True)
class IntroductionLabel:
    """Who an introduction is with and about: the names a conversation list shows."""

    organization_id: UUID
    organization_name: str
    role_id: UUID
    role_title: str


def introduction_labels(conn: Connection, introduction_ids: Sequence[UUID]) -> dict[UUID, IntroductionLabel]:
    """Organization and role names of the introductions this connection can see, by introduction id; the rest are left
    out. The candidate reads them through introduction_role() (a candidate has no access to roles or organizations), a
    member of the organization or its token (which may read its own organization and roles) from the tables, so each
    caller gets exactly the introductions its row-level security shows."""
    if not introduction_ids:
        return {}
    rows = conn.execute(
        text(
            """SELECT i.id, i.organization_id, i.role_id,
                      COALESCE(ir.organization_name, o.name) AS organization_name,
                      COALESCE(ir.role_title, r.title) AS role_title
                 FROM unnest(CAST(:ids AS uuid[])) AS t(id)
                 JOIN introductions i ON i.id = t.id
                 LEFT JOIN LATERAL introduction_role(i.id) ir ON true
                 LEFT JOIN organizations o ON o.id = i.organization_id
                 LEFT JOIN roles r ON r.id = i.role_id"""
        ),
        {"ids": list(introduction_ids)},
    )
    return {
        r.id: IntroductionLabel(r.organization_id, r.organization_name, r.role_id, r.role_title)
        for r in rows
        if r.organization_name is not None and r.role_title is not None
    }
