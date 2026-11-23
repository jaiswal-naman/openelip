"""Who may touch an organization: the two checks every hiring route starts with (HIR-01).

A caller who is not an active member of the organization (a stranger, a person who is only invited, a removed member)
gets 404 not_found, never 403: an organization that is not theirs is not theirs to know about. Only a member who is not
an owner or an admin gets 403 forbidden, because they can already see the organization.

Both read the signed-in person from the transaction (db.transaction sets app.user_id), so the connection must come from
a request's transaction.
"""

from typing import cast
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform.errors import ApiError

from .models import OrgRole


def require_member(conn: Connection, org_id: UUID) -> OrgRole:
    """The caller's role in the organization, or 404 when they are not an active member of it."""
    role = conn.execute(
        text(
            """SELECT role::text FROM org_members
               WHERE organization_id = :org AND user_id = app_user_id() AND status = 'active'"""
        ),
        {"org": org_id},
    ).scalar_one_or_none()
    if role is None:
        raise ApiError(404, "not_found", "No such resource.")
    return cast(OrgRole, role)


def require_admin(conn: Connection, org_id: UUID) -> OrgRole:
    """The caller's role, when it is owner or admin. A plain member gets 403, anyone else 404."""
    role = require_member(conn, org_id)
    if role == "member":
        raise ApiError(403, "forbidden", "Only an owner or an admin of the organization can do this.")
    return role
