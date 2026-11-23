"""Organizations and their members: create, invite, join, change a role, remove, claim ownership (HIR-01; plan 06-03).

Every function takes the request's connection (app_api, with app.user_id set) and the signed-in person's id. Row-level
security keeps one organization out of another and `access` turns "not a member" into 404; what the database cannot say
is enforced here, inside the same transaction with the organization's member rows locked `FOR UPDATE`:

  * only an owner may make an owner, or change or remove one (an admin may manage members and other admins);
  * the last active owner can be neither demoted nor removed (409 conflict, state last_owner);
  * an admin may claim ownership of an organization that has no active owner (409 conflict, state has_owner otherwise);
  * a pending invitation and an active member of the same address are 409 conflict (states invited and member).

The invitation email is one outbox row per invitation, written in the invite's own transaction, so the invitation and
its email commit together or not at all (the dedupe key makes a repeat harmless).
"""

from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.platform import outbox
from app.platform.errors import ApiError
from app.trust.api import audit_log

from .access import require_admin, require_member

INVITATION_TEMPLATE = "org_invitation_v1"
MAX_NAME = 120  # assumption: the longest organization name


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such resource.")


def _conflict(state: str, message: str) -> ApiError:
    return ApiError(409, "conflict", message, {"state": state})


def valid_timezone(name: str) -> bool:
    """An IANA zone the database and the answer clock can use (the database does not check it: 06-01 Facts)."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


# ---- reading -------------------------------------------------------------------------------------------------------


def get(conn: Connection, caller: UUID, org_id: UUID) -> dict[str, Any]:
    """The organization, its time zone and its members with pending invitations. 404 for anyone who is not a member."""
    my_role = require_member(conn, org_id)
    org = conn.execute(
        text("SELECT id, name, timezone, is_synthetic FROM organizations WHERE id = :id"), {"id": org_id}
    ).one_or_none()
    if org is None:
        raise _not_found()
    members = conn.execute(
        text(
            """SELECT member_id, user_id, display_name, email, role::text AS role, status::text AS status
               FROM org_directory(:id) ORDER BY status, role, email, member_id"""
        ),
        {"id": org_id},
    )
    return {
        "id": org.id,
        "name": org.name,
        "timezone": org.timezone,
        "is_synthetic": org.is_synthetic,
        "role": my_role,
        "members": [dict(row._mapping) for row in members],
    }


# ---- creating and joining ------------------------------------------------------------------------------------------


def create(conn: Connection, caller: UUID, *, name: str, timezone: str) -> dict[str, Any]:
    """A new organization with the caller as its owner."""
    org_id: UUID = conn.execute(
        text("SELECT create_organization(:name, :tz)"), {"name": name.strip(), "tz": timezone}
    ).scalar_one()
    return get(conn, caller, org_id)


def invite(conn: Connection, caller: UUID, org_id: UUID, *, email: str, role: str) -> dict[str, Any]:
    """Invite an address as member or admin; one email is queued for the invitation. An owner or admin only (403 for a
    member). 409 conflict: state `invited` when the address already has a pending invitation, `member` when it already
    belongs to an active member."""
    require_admin(conn, org_id)
    address = email.strip().lower()
    taken = conn.execute(
        text(
            """SELECT status::text FROM org_directory(:org)
               WHERE lower(email) = :email AND status IN ('active', 'invited') ORDER BY status LIMIT 1"""
        ),
        {"org": org_id, "email": address},
    ).scalar_one_or_none()
    if taken == "active":
        raise _conflict("member", "That address already belongs to a member of this organization.")
    if taken == "invited":
        raise _conflict("invited", "That address has already been invited.")
    try:
        with conn.begin_nested():  # a lost race on the unique index must not poison the request's transaction
            member_id: UUID = conn.execute(
                text(
                    """INSERT INTO org_members (organization_id, invited_email, role, status, invited_by)
                       VALUES (:org, :email, CAST(:role AS org_role), 'invited', :caller) RETURNING id"""
                ),
                {"org": org_id, "email": address, "role": role, "caller": caller},
            ).scalar_one()
    except IntegrityError as exc:
        raise _conflict("invited", "That address has already been invited.") from exc
    outbox.add(
        conn,
        kind=INVITATION_TEMPLATE,
        dedupe_key=f"org_invitation:{member_id}",
        user_id=None,  # the invitee may have no account yet; the relay reads the address at send time
        payload={"member_id": str(member_id)},
    )
    audit_log(
        conn,
        "hiring.member_invited",
        "employer",
        None,
        {"member_id": str(member_id), "role": role},
        actor_user_id=caller,
        entity_table="org_members",
        entity_id=str(member_id),
        organization_id=org_id,
    )
    return {
        "member_id": member_id,
        "user_id": None,
        "display_name": None,
        "email": address,
        "role": role,
        "status": "invited",
    }


def join(conn: Connection, caller: UUID, org_id: UUID) -> dict[str, Any]:
    """Accept the invitation to this organization sent to the caller's own address. 404 when there is none (another
    address, already accepted, revoked): an invitation that is not yours is not yours to know about."""
    member_id = conn.execute(
        text("SELECT member_id FROM my_org_invitations() WHERE organization_id = :org LIMIT 1"), {"org": org_id}
    ).scalar_one_or_none()
    if member_id is None:
        raise _not_found()
    try:
        with conn.begin_nested():
            conn.execute(text("SELECT accept_org_invitation(:id)"), {"id": member_id})
    except DBAPIError as exc:  # the function raises insufficient_privilege when the invitation just went away
        if getattr(exc.orig, "sqlstate", None) == "42501":
            raise _not_found() from exc
        raise
    return get(conn, caller, org_id)


# ---- members -------------------------------------------------------------------------------------------------------


def _lock_members(conn: Connection, org_id: UUID) -> list[Any]:
    """Every active or invited member row of the organization, locked, so the owner counts below cannot race."""
    return list(
        conn.execute(
            text(
                """SELECT id, user_id, role::text AS role, status::text AS status FROM org_members
                   WHERE organization_id = :org AND status IN ('active', 'invited') ORDER BY id FOR UPDATE"""
            ),
            {"org": org_id},
        )
    )


def _target(rows: list[Any], member_id: UUID) -> Any:
    found = next((row for row in rows if row.id == member_id), None)
    if found is None:
        raise _not_found()
    return found


def _active_owners(rows: list[Any]) -> int:
    return sum(1 for row in rows if row.role == "owner" and row.status == "active")


def _only_an_owner_touches_owners(caller_role: str, target: Any, new_role: str | None) -> None:
    if caller_role != "owner" and (target.role == "owner" or new_role == "owner"):
        raise ApiError(403, "forbidden", "Only an owner can make, change or remove an owner.")


def change_role(conn: Connection, caller: UUID, org_id: UUID, member_id: UUID, *, role: str) -> dict[str, Any]:
    """Set a member's role. An owner or admin only; making someone owner (or changing an owner) needs an owner; the last
    active owner cannot be demoted (409 conflict, state last_owner); an invited person cannot be made owner."""
    caller_role = require_admin(conn, org_id)
    rows = _lock_members(conn, org_id)
    target = _target(rows, member_id)
    _only_an_owner_touches_owners(caller_role, target, role)
    if role == "owner" and target.status != "active":
        raise _conflict("invited", "A person who has not joined yet cannot be made an owner.")
    if target.role == "owner" and role != "owner" and target.status == "active" and _active_owners(rows) == 1:
        raise _conflict("last_owner", "An organization needs an owner: make someone else an owner first.")
    conn.execute(
        text("UPDATE org_members SET role = CAST(:role AS org_role) WHERE id = :id"), {"role": role, "id": member_id}
    )
    return _member(conn, org_id, member_id)


def remove(conn: Connection, caller: UUID, org_id: UUID, member_id: UUID) -> None:
    """Remove a member or revoke a pending invitation. An owner or admin only; removing an owner needs an owner; the
    last active owner cannot be removed (409 conflict, state last_owner). A removed person gets 404 from then on."""
    caller_role = require_admin(conn, org_id)
    rows = _lock_members(conn, org_id)
    target = _target(rows, member_id)
    _only_an_owner_touches_owners(caller_role, target, None)
    if target.role == "owner" and target.status == "active" and _active_owners(rows) == 1:
        raise _conflict("last_owner", "An organization needs an owner: make someone else an owner first.")
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": member_id}
    )
    audit_log(
        conn,
        "hiring.member_removed",
        "employer",
        None,
        {"member_id": str(member_id), "was": target.status},
        actor_user_id=caller,
        entity_table="org_members",
        entity_id=str(member_id),
        organization_id=org_id,
    )


def claim(conn: Connection, caller: UUID, org_id: UUID) -> dict[str, Any]:
    """An active admin makes themselves owner of an organization that has no active owner (DATA_MODEL issue 18).
    409 conflict, state has_owner, when there is one. A member who is not an admin gets 403."""
    require_admin(conn, org_id)
    rows = _lock_members(conn, org_id)
    if _active_owners(rows) > 0:
        raise _conflict("has_owner", "This organization already has an owner.")
    mine = next(row for row in rows if row.user_id == caller and row.status == "active")
    conn.execute(text("UPDATE org_members SET role = 'owner' WHERE id = :id"), {"id": mine.id})
    return get(conn, caller, org_id)


def _member(conn: Connection, org_id: UUID, member_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        text(
            """SELECT member_id, user_id, display_name, email, role::text AS role, status::text AS status
               FROM org_directory(:org) WHERE member_id = :id"""
        ),
        {"org": org_id, "id": member_id},
    ).one_or_none()
    if row is None:
        raise _not_found()
    return dict(row._mapping)
