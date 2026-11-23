"""The organization routes, /v1/orgs: create, get, invite by email, join, change a role, remove or revoke, claim
ownership (API.md 2.15; HIR-01; plan 06-03). The answer clock (GET /v1/orgs/{id}/answer-clock) belongs to plan 06-04.

    POST   /v1/orgs                                  create an organization; the caller becomes its owner
    GET    /v1/orgs/{id}                             the organization, its time zone and its members
    POST   /v1/orgs/{id}/members                     invite an address as member or admin (one email)
    POST   /v1/orgs/{id}/join                        accept the invitation sent to the caller's own address
    PATCH  /v1/orgs/{id}/members/{member_id}         change a role (an owner makes owners)
    DELETE /v1/orgs/{id}/members/{member_id}         remove a member or revoke an invitation
    POST   /v1/orgs/{id}/claim-ownership             an admin takes ownership of an organization with no owner

Members are addressed by member id, not user id, so a pending invitation (no user yet) can be revoked (INDEX deviation
17). Another organization is a 404, never a 403: the organization is not the caller's to know about.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import orgs
from .models import MemberStatus, OrgRole

router = APIRouter()

EMAIL_PATTERN = (
    r"^[^@\s]+@[^@\s]+$"  # a plausible address, as for sign-in (the invitee proves it by signing in with it)
)

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


# ---- what the API takes --------------------------------------------------------------------------------------------


class OrgCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=orgs.MAX_NAME)
    timezone: str = Field(min_length=1, max_length=64)  # an IANA zone name: business days are counted in it

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("a name cannot be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        if not orgs.valid_timezone(value):
            raise ValueError("not an IANA time zone, for example Europe/Berlin")
        return value


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    role: Literal["member", "admin"] = "member"


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: OrgRole


# ---- what the API returns ------------------------------------------------------------------------------------------


class Member(BaseModel):
    member_id: UUID
    user_id: UUID | None  # null for a pending invitation
    display_name: str | None
    email: str | None
    role: OrgRole
    status: MemberStatus  # invited or active (removed members are not listed)


class Organization(BaseModel):
    id: UUID
    name: str
    timezone: str
    is_synthetic: bool  # a demo organization, labelled wherever it is shown
    role: OrgRole  # the caller's own role
    members: list[Member]


def _answer(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """A write's answer as the declared model serializes it, so it cannot drift from the contract."""
    return model.model_validate(data).model_dump(mode="json")


# ---- routes --------------------------------------------------------------------------------------------------------


@router.post("/v1/orgs", status_code=201, response_model=Envelope[Organization])
def create_org(body: OrgCreate, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(Organization, orgs.create(conn, write.user_id, name=body.name, timezone=body.timezone))

    return write.run(effect, status=201)


@router.get("/v1/orgs/{org_id}", response_model=Envelope[Organization])
def get_org(org_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        found = orgs.get(conn, principal.user_id, org_id)
    return envelope(_answer(Organization, found))


@router.post("/v1/orgs/{org_id}/members", status_code=201, response_model=Envelope[Member])
def invite_member(org_id: UUID, body: InviteBody, write: Idempotent) -> Response:
    """Invite an address. One email is queued with the invitation. 403 for a plain member; 409 conflict, state invited
    (already pending) or member (already belongs)."""

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(Member, orgs.invite(conn, write.user_id, org_id, email=body.email, role=body.role))

    return write.run(effect, status=201)


@router.post("/v1/orgs/{org_id}/join", response_model=Envelope[Organization])
def join_org(org_id: UUID, write: Idempotent) -> Response:
    """Accept the invitation sent to the caller's own address; any other caller gets 404."""

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(Organization, orgs.join(conn, write.user_id, org_id))

    return write.run(effect)


@router.patch("/v1/orgs/{org_id}/members/{member_id}", response_model=Envelope[Member])
def change_member_role(org_id: UUID, member_id: UUID, body: RoleBody, write: Idempotent) -> Response:
    """Change a role. 403 for a plain member, or for an admin who touches an owner; 409 conflict, state last_owner."""

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(Member, orgs.change_role(conn, write.user_id, org_id, member_id, role=body.role))

    return write.run(effect)


@router.delete("/v1/orgs/{org_id}/members/{member_id}", status_code=204)
def remove_member(org_id: UUID, member_id: UUID, write: Idempotent) -> Response:
    """Remove a member or revoke an invitation. 409 conflict, state last_owner, for the last owner."""

    def effect(conn: Connection) -> None:
        orgs.remove(conn, write.user_id, org_id, member_id)

    return write.run(effect, status=204)


@router.post("/v1/orgs/{org_id}/claim-ownership", response_model=Envelope[Organization])
def claim_ownership(org_id: UUID, write: Idempotent) -> Response:
    """An active admin becomes owner of an organization with no active owner; else 409 conflict, state has_owner."""

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(Organization, orgs.claim(conn, write.user_id, org_id))

    return write.run(effect)
