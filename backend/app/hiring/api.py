"""What other modules may call from app.hiring (the shared contracts in the Phase 6 index). Nothing else in this
package is a public interface.

  my_organizations(conn, user_id)         the organizations a person belongs to and the invitations waiting for them
  asks_for_brief(conn, user_id)           the person's open asks, newest first (the worker side: the brief's compose)
  brief_asks(conn, introduction_ids)      organization, role and expiry of the caller's own introductions
  introduction_ref(conn, introduction_id) who an introduction is between and where it stands, for campaigns (Phase 7)
  introduction_labels(conn, ids)          organization and role names of the introductions the caller can see (Phase 9)
  accepted_introductions(conn, ...)       an organization's accepted introductions for an outside agent (Phase 9)
  queue_ordinary_ask(conn, ...)           queue the ordinary ask for an open role, as the publish route does (Phase 9)

Membership, Invitation and BriefAsk are what they return.
"""

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import queue

from .queries import Invitation, Membership, my_organizations

__all__ = [
    "AcceptedIntroduction",
    "AnswersOverdue",
    "AskQueued",
    "BadCursor",
    "BriefAsk",
    "IntroductionLabel",
    "IntroductionRef",
    "Invitation",
    "Membership",
    "RoleNotFound",
    "RoleNotOpen",
    "accepted_introductions",
    "asks_for_brief",
    "brief_asks",
    "introduction_labels",
    "introduction_ref",
    "my_organizations",
    "queue_ordinary_ask",
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


@dataclass(frozen=True)
class AcceptedIntroduction:
    """One accepted introduction as an employer's agent reads it: the role, the answer state, what the candidate agreed
    to share (the frozen copy, third-party text) and the email only after the organization answered yes."""

    introduction_id: UUID
    role_id: UUID
    role_title: str
    responded_at: datetime
    answer_due_at: datetime | None
    is_overdue: bool
    employer_answer: str | None
    shared: dict[str, Any] | None
    email: str | None


class BadCursor(ValueError):
    """A cursor this list did not issue."""


def _encode(when: datetime, ident: UUID) -> str:
    raw = json.dumps([when.isoformat(), str(ident)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        when, ident = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(when), UUID(ident)
    except (ValueError, TypeError):
        raise BadCursor("not a cursor of this list") from None


def accepted_introductions(
    conn: Connection,
    *,
    organization_id: UUID,
    role_id: UUID | None = None,
    before: str | None = None,
    limit: int = 20,
) -> tuple[list[AcceptedIntroduction], str | None]:
    """The organization's accepted introductions, newest answer first, one page, and the cursor of the next. Only
    accepted rows exist for this reader: the caller's row-level security (a member, or an organization token that holds
    twin:ask or introductions:request) shows no asked, declined or expired one, so nothing here reveals who was asked.
    The email is the preview's, and only once the answer is yes. `before` is a cursor from an earlier call; a cursor
    this list did not issue raises BadCursor."""
    params: dict[str, Any] = {"org": organization_id, "role": role_id, "limit": limit + 1}
    after = ""
    if before is not None:
        params["c_at"], params["c_id"] = _decode(before)
        after = "AND (i.responded_at, i.id) < (:c_at, :c_id)"
    rows = conn.execute(
        text(
            f"""SELECT i.id, i.role_id, r.title AS role_title, i.responded_at, i.answer_due_at,
                       (i.employer_answer IS NULL AND i.answer_due_at < now()) AS is_overdue,
                       i.employer_answer::text AS employer_answer, i.share_preview
                  FROM introductions i JOIN roles r ON r.id = i.role_id
                 WHERE i.organization_id = :org AND i.status = 'accepted'
                   AND (CAST(:role AS uuid) IS NULL OR i.role_id = :role) {after}
                 ORDER BY i.responded_at DESC, i.id DESC LIMIT :limit"""
        ),
        params,
    ).all()
    page = rows[:limit]
    found = []
    for row in page:
        preview = row.share_preview or {}
        email = (preview.get("on_employer_yes") or {}).get("email") if row.employer_answer == "yes" else None
        found.append(
            AcceptedIntroduction(
                introduction_id=row.id,
                role_id=row.role_id,
                role_title=row.role_title,
                responded_at=row.responded_at,
                answer_due_at=row.answer_due_at,
                is_overdue=bool(row.is_overdue),
                employer_answer=row.employer_answer,
                shared=preview.get("on_accept"),
                email=email,
            )
        )
    next_cursor = _encode(page[-1].responded_at, page[-1].id) if len(rows) > limit else None
    return found, next_cursor


class RoleNotFound(Exception):
    """No such role in this organization, as far as the caller may see."""


class RoleNotOpen(Exception):
    """The role is a draft or closed, or has no published job."""


class AnswersOverdue(Exception):
    """The organization owes an answer, so it may ask nobody new."""


@dataclass(frozen=True)
class AskQueued:
    """What starting the ordinary ask returned: when, and how many candidates are being asked, as a bucket only."""

    candidates_asked: str  # under_5, 5_to_9, 10_to_24 or 25_plus (role_ask_bucket)
    ran_at: datetime


def queue_ordinary_ask(conn: Connection, *, organization_id: UUID, role_id: UUID, unique_key: str) -> AskQueued:
    """Queue Phase 6's hiring.ask_candidates for an open role of the organization, as publishing does, under the
    caller's row-level security. A repeated unique key queues nothing new. The count is the bucket of asks still waiting
    on candidates when this is called (role_ask_bucket); the asks the new run makes show in later calls. No
    introduction, approval or share is created here: the worker's run asks, and each candidate decides."""
    role = conn.execute(
        text("SELECT r.status::text AS status, r.job_id FROM roles r WHERE r.id = :r AND r.organization_id = :o"),
        {"r": role_id, "o": organization_id},
    ).one_or_none()
    if role is None:
        raise RoleNotFound
    if role.status != "open" or role.job_id is None:
        raise RoleNotOpen
    if conn.execute(text("SELECT organization_has_overdue_answers(:org)"), {"org": organization_id}).scalar_one():
        raise AnswersOverdue
    queue.enqueue(conn, "hiring.ask_candidates", {"role_id": str(role_id)}, unique_key=unique_key, queue="llm")
    bucket = conn.execute(text("SELECT role_ask_bucket(:r)"), {"r": role_id}).scalar_one()
    return AskQueued(candidates_asked=bucket, ran_at=datetime.now(UTC))
