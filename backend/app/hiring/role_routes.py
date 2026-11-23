"""The role routes, /v1/roles: create with extraction, list, get, patch, requirements, publish, close, preview (API.md
2.16; HIR-02, HIR-03; plan 06-03). The shortlist route belongs to plan 06-04.

    POST /v1/roles                       describe a role in text; requirements are read from it; starts as draft
    GET  /v1/roles?org_id=               the organization's roles, with what is owed on each
    GET  /v1/roles/{id}                  one role with requirements, pay range, can_publish and blockers
    PATCH /v1/roles/{id}                 change fields of a draft (a changed description is read again)
    PUT  /v1/roles/{id}/requirements     replace and confirm the requirement list
    POST /v1/roles/{id}/publish          open the role as an unlisted job (422 blockers, 409 answers_overdue)
    POST /v1/roles/{id}/close            close it; open asks are withdrawn
    GET  /v1/roles/{id}/preview          counts only, 1 to 4 shown as "fewer than 5" (0 too for the two reply counts), never a person

The models are the contract: the web types are generated from them. A handler is a thin call into hiring.roles. A role
of another organization is a 404 (row-level security hides it). Role text is employer-written and is marked
x-untrusted-text: the web shows it as plain text (API.md 1.10). There is no version or ETag on a role (INDEX deviation
5): drafts are last write wins.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import JsonDict
from sqlalchemy import Connection, text

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import roles
from .models import (
    EmploymentType,
    RequirementImportance,
    RequirementKind,
    RoleStatus,
    Seniority,
    VisaSponsorship,
    WorkArrangement,
)

router = APIRouter()

MAX_TITLE = 200  # assumption: the longest role title
MAX_DESCRIPTION = 20_000  # assumption: the longest role description, in characters
MAX_REQUIREMENT_TEXT = 1_000  # assumption: the longest requirement
MAX_SALARY = 2_000_000_000  # roles.salary_* are 32-bit integers (annual, whole currency units)
UNTRUSTED: JsonDict = {"x-untrusted-text": True}
PREVIEW_NOTE = "Counts only. Groups smaller than 5 are hidden. You see a person only after they accept."
SUPPRESSED_BELOW = 5  # assumption, held by suppress_small() in the database

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


def _no_nul(value: str) -> str:
    if "\x00" in value:  # PostgreSQL text cannot hold NUL
        raise ValueError("text cannot hold a NUL character")
    return value


# ---- what the API takes --------------------------------------------------------------------------------------------


class PayRange(BaseModel):
    """Annual pay in whole currency units (API.md 1.4)."""

    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=0, le=MAX_SALARY)
    max: int = Field(ge=0, le=MAX_SALARY)
    currency: str = Field(pattern=r"^[A-Z]{3}$")  # ISO 4217

    @model_validator(mode="after")
    def _min_is_not_above_max(self) -> Self:
        if self.min > self.max:
            raise ValueError("min cannot be greater than max")
        return self


class RoleFields(BaseModel):
    """What an employer sets besides the text; nothing is inferred from the description (INDEX deviation 6)."""

    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
    city: str | None = Field(default=None, min_length=1, max_length=120)
    work_arrangements: list[WorkArrangement] | None = Field(default=None, max_length=3)
    employment_type: EmploymentType | None = None
    seniority: Seniority | None = None
    visa_sponsorship: VisaSponsorship | None = None

    @field_validator("city")
    @classmethod
    def _city_text(cls, value: str | None) -> str | None:
        return None if value is None else _no_nul(value.strip()) or None


class RoleCreate(RoleFields):
    model_config = ConfigDict(extra="forbid")

    org_id: UUID
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION, json_schema_extra=UNTRUSTED)
    pay_range: PayRange | None = None

    @field_validator("title", "description")
    @classmethod
    def _text(cls, value: str) -> str:
        if not _no_nul(value).strip():
            raise ValueError("cannot be blank")
        return value


class RolePatch(RoleFields):
    """Only the fields sent change. title and description cannot be cleared; pay_range, country and city can (null)."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    description: str | None = Field(default=None, min_length=1, max_length=MAX_DESCRIPTION, json_schema_extra=UNTRUSTED)
    pay_range: PayRange | None = None

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self) -> Self:
        cleared = [
            name
            for name in (
                "title",
                "description",
                "work_arrangements",
                "employment_type",
                "seniority",
                "visa_sponsorship",
            )
            if name in self.model_fields_set and getattr(self, name) is None
        ]
        if cleared:
            raise ValueError(f"{', '.join(cleared)} cannot be null")
        for name in ("title", "description"):
            value = getattr(self, name)
            if value is not None and not _no_nul(value).strip():
                raise ValueError(f"{name} cannot be blank")
        return self

    def changes(self) -> dict[str, Any]:
        """The fields the caller sent, as the service takes them (pay_range as a dict or None)."""
        found: dict[str, Any] = {}
        for name in self.model_fields_set:
            value = getattr(self, name)
            found[name] = value.model_dump() if isinstance(value, PayRange) else value
        return found


class SpanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)  # Unicode code points, end exclusive
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _a_span_has_length(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class RequirementIn(BaseModel):
    """One line of the list the employer confirms. An item typed by hand sends no span; an extracted one may send its
    span back, and keeps it only while its text is still the description at that span."""

    model_config = ConfigDict(extra="forbid")

    kind: RequirementKind = "other"
    text: str = Field(min_length=1, max_length=MAX_REQUIREMENT_TEXT, json_schema_extra=UNTRUSTED)
    importance: RequirementImportance
    is_confirmed: bool
    span: SpanIn | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        if not _no_nul(value).strip():
            raise ValueError("cannot be blank")
        return value


class RequirementsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[RequirementIn] = Field(max_length=roles.MAX_REQUIREMENTS)


# ---- what the API returns ------------------------------------------------------------------------------------------


class CharSpan(BaseModel):
    start: int  # Unicode code points, end exclusive
    end: int


class RequirementView(BaseModel):
    id: UUID
    kind: RequirementKind
    text: str = Field(json_schema_extra=UNTRUSTED)
    importance: RequirementImportance
    span: CharSpan | None  # null for an item the employer typed or edited
    quote: str | None = Field(json_schema_extra=UNTRUSTED)  # the description's own words at the span
    is_confirmed: bool


class Extraction(BaseModel):
    state: Literal["extracted", "empty", "ai_disabled", "failed"]
    dropped: int  # items beyond the role's cap of 30


class RoleView(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    description: str = Field(json_schema_extra=UNTRUSTED)
    status: RoleStatus
    country: str | None
    city: str | None
    work_arrangements: list[WorkArrangement]
    employment_type: EmploymentType
    seniority: Seniority
    visa_sponsorship: VisaSponsorship
    pay_range: PayRange | None
    job_id: int | None  # the unlisted index row, once the role is open
    requirements: list[RequirementView]
    can_publish: bool
    blockers: list[Literal["pay_range_required", "requirements_unconfirmed"]]
    created_at: datetime
    opened_at: datetime | None
    closed_at: datetime | None
    extraction: Extraction | None = None  # only in the answers to create and to a description change


class RoleListItem(BaseModel):
    id: UUID
    title: str
    status: RoleStatus
    said_yes: int
    answers_owed: int
    answers_overdue: int
    created_at: datetime
    updated_at: datetime  # the latest of created, opened and closed: edits to a draft are not tracked


Count = int | Literal["fewer than 5"]


class Preview(BaseModel):
    """Counts only (API.md 3.11, INDEX deviation 8). 1 to 4 are shown as "fewer than 5"; said_yes is exact because each
    of them is already on the shortlist. No identifier of a person appears anywhere."""

    role_id: UUID
    as_of: datetime | None  # when the latest finished role run ended; null before the first one
    eligible_candidates: Count
    selected_to_ask: Count
    waiting_for_reply: Count
    said_yes: int
    said_no_or_expired: Count
    suppressed_below: int
    note: str


def _answer(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """A write's answer as the declared model serializes it, so it cannot drift from the contract."""
    return model.model_validate(data).model_dump(mode="json")


# ---- routes --------------------------------------------------------------------------------------------------------


@router.post("/v1/roles", status_code=201, response_model=Envelope[RoleView])
def create_role(body: RoleCreate, write: Idempotent) -> Response:
    """Describe a role. The requirements are read from the description (synchronously); with no model the role is
    stored with none and extraction.state says ai_disabled, so the employer types them."""

    def effect(conn: Connection) -> dict[str, Any]:
        fields = {
            name: getattr(body, name)
            for name in RoleFields.model_fields
            if getattr(body, name) is not None  # an unset field keeps the column's default
        }
        made = roles.create(
            conn,
            write.user_id,
            org_id=body.org_id,
            title=body.title,
            description=body.description,
            fields=fields,
            pay_range=body.pay_range.model_dump() if body.pay_range else None,
        )
        return _answer(RoleView, made)

    return write.run(effect, status=201)


@router.get("/v1/roles", response_model=Envelope[list[RoleListItem]])
def list_roles(principal: Signed, org_id: Annotated[UUID, Query()]) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        listed = roles.list_for_org(conn, principal.user_id, org_id)
    return envelope([_answer(RoleListItem, row) for row in listed])


@router.get("/v1/roles/{role_id}", response_model=Envelope[RoleView])
def get_role(role_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        found = roles.get(conn, principal.user_id, role_id)
    return envelope(_answer(RoleView, found))


@router.patch("/v1/roles/{role_id}", response_model=Envelope[RoleView])
def patch_role(role_id: UUID, body: RolePatch, write: Idempotent) -> Response:
    """Change a draft. An open or closed role is 409 conflict (state open or closed)."""

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(RoleView, roles.update(conn, write.user_id, role_id, body.changes()))

    return write.run(effect)


@router.put("/v1/roles/{role_id}/requirements", response_model=Envelope[RoleView])
def put_requirements(role_id: UUID, body: RequirementsPut, write: Idempotent) -> Response:
    """Replace the draft's requirement list. It counts as confirmed when it is not empty and every item is."""
    items = [
        roles.RequirementInput(
            item.kind,
            item.text,
            item.importance,
            item.is_confirmed,
            (item.span.start, item.span.end) if item.span else None,
        )
        for item in body.requirements
    ]

    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(RoleView, roles.set_requirements(conn, write.user_id, role_id, items))

    return write.run(effect)


@router.post("/v1/roles/{role_id}/publish", response_model=Envelope[RoleView])
def publish_role(role_id: UUID, write: Idempotent) -> Response:
    """Open the role as an unlisted job and queue the ask. 422 pay_range_required or requirements_unconfirmed (every
    blocker in details.blockers); 409 conflict while answers are overdue or when the role is not a draft."""

    def effect(conn: Connection) -> dict[str, Any]:
        roles.publish(write.user_id, role_id)  # its own worker transaction; the answer is read back as the caller
        return _answer(RoleView, roles.get(conn, write.user_id, role_id))

    return write.run(effect)


@router.post("/v1/roles/{role_id}/close", response_model=Envelope[RoleView])
def close_role(role_id: UUID, write: Idempotent) -> Response:
    """Close an open role: asks still waiting are withdrawn; accepted introductions keep their answer obligation."""

    def effect(conn: Connection) -> dict[str, Any]:
        roles.close(write.user_id, role_id)
        return _answer(RoleView, roles.get(conn, write.user_id, role_id))

    return write.run(effect)


@router.get("/v1/roles/{role_id}/preview", response_model=Envelope[Preview])
def role_preview(role_id: UUID, principal: Signed) -> dict[str, Any]:
    """The anonymous preview. role_preview() answers only for a member of the role's organization (NULL otherwise)."""
    with db.transaction(principal.user_id) as conn:
        counts = conn.execute(text("SELECT role_preview(:id)"), {"id": role_id}).scalar_one()
    if counts is None:
        raise ApiError(404, "not_found", "No such resource.")
    return envelope(
        _answer(Preview, {"role_id": role_id, **counts, "suppressed_below": SUPPRESSED_BELOW, "note": PREVIEW_NOTE})
    )
