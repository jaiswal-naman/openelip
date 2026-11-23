"""The reads other modules need from the hiring desk (re-exported by app.hiring.api):

  my_organizations(conn, user_id)   the employer organizations a person belongs to and the invitations waiting for them

It takes an open connection and returns plain frozen values, so the caller owns the transaction. It must run as the
person (app.user_id is theirs, as in any request): the invitations are found through my_org_invitations(), which reads
the caller's address, because an invited person is not a member yet and row-level security hides the invitation row.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, text

from .models import OrgRole


@dataclass(frozen=True)
class Membership:
    """An organization the person is an active member of, with their role in it."""

    id: UUID
    name: str
    role: OrgRole
    is_synthetic: bool  # a demo organization, labelled wherever it is shown


@dataclass(frozen=True)
class Invitation:
    """An open invitation to the person's address. member_id is the invitation row, which joining uses."""

    member_id: UUID
    organization_id: UUID
    organization_name: str
    role: OrgRole
    invited_at: datetime


def my_organizations(conn: Connection, user_id: UUID) -> tuple[list[Membership], list[Invitation]]:
    """(memberships, invitations) of the signed-in person: their active memberships by joining time, and the open
    invitations to their address by sending time. Removed members and other addresses' invitations are not listed."""
    memberships = [
        Membership(row.id, row.name, cast(OrgRole, row.role), row.is_synthetic)
        for row in conn.execute(
            text(
                """SELECT o.id, o.name, m.role::text AS role, o.is_synthetic
                   FROM org_members m JOIN organizations o ON o.id = m.organization_id
                   WHERE m.user_id = :user AND m.status = 'active'
                   ORDER BY m.joined_at, o.name, o.id"""
            ),
            {"user": user_id},
        )
    ]
    invitations = [
        Invitation(row.member_id, row.organization_id, row.organization_name, cast(OrgRole, row.role), row.created_at)
        for row in conn.execute(
            text(
                """SELECT member_id, organization_id, organization_name, role::text AS role, created_at
                   FROM my_org_invitations()"""
            )
        )
    ]
    return memberships, invitations
