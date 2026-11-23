"""The Career Record under /v1/me (API.md 2.3): the whole record, facts, hard constraints, soft preferences and the
change log with its proposals. The routes and their Pydantic models are the contract: the web types are generated
from them.

A route translates HTTP to one service call. The person is the session's, never a field of a body or a path (T-3-12): a
row of someone else is a 404 because every query filters by the session's user id. Nothing here needs the ai_processing
consent: the record works without it. Every write needs an Idempotency-Key, and a repeat gets the first answer.

API.md 1.7 (If-Match) is optional and not built: record_version is returned by every write and by GET /v1/me/record.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import JsonDict
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import api, documents_routes, events, service
from .errors import InvalidField
from .models import (
    ConstraintKind,
    EmploymentType,
    FactKind,
    PreferenceKind,
    ProfileEntity,
    ProfileEventType,
    ProfileOperation,
    ProposalOrigin,
    RecordSource,
    Seniority,
    WorkArrangement,
)

router = APIRouter(prefix="/v1/me")
router.include_router(documents_routes.router)  # POST /documents and GET /documents/{id} (resume upload)

MAX_DECISIONS = 100  # facts one review request may decide
FactStatus = Literal["unconfirmed", "confirmed", "rejected", "superseded"]
DocumentStatus = Literal["queued", "running", "succeeded", "failed"]
# Text from the candidate's own file: clients show it as text and never as markup (API.md 1.10).
UNTRUSTED: JsonDict = {"x-untrusted-text": True}

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]
PageSize = Annotated[int, Query(ge=1, le=100)]
After = Annotated[str | None, Query(max_length=400)]  # the opaque cursor from meta.next_cursor


# ---- what the API returns ------------------------------------------------------------------------


class FactSpan(BaseModel):
    start: int
    end: int


class FactProvenance(BaseModel):
    """Where a resume fact came from: the document, the character span in its text and the text at that span."""

    document_id: UUID
    span: FactSpan
    quote: str = Field(json_schema_extra=UNTRUSTED)


class RecordFact(BaseModel):
    id: UUID
    kind: FactKind
    status: FactStatus
    statement: str = Field(json_schema_extra=UNTRUSTED)
    attributes: dict[str, Any]
    source: RecordSource
    parent_fact_id: UUID | None
    provenance: FactProvenance | None  # null for a fact the candidate wrote
    created_at: datetime


class SalaryFloor(BaseModel):
    amount: int = Field(gt=0, le=service.MAX_INT4)  # whole currency units per year
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class ConstraintFields(BaseModel):
    """The typed value of a hard constraint. A kind uses only its own fields (DATA_MODEL 3.4): location uses
    country_codes and cities, work_authorization country_codes, remote_policy arrangements, employment_type
    employment_types, seniority seniorities, salary_floor salary_floor and excluded_company company_names."""

    country_codes: list[str] | None = Field(default=None, max_length=service.MAX_ITEMS)
    cities: list[str] | None = Field(default=None, max_length=service.MAX_ITEMS)
    arrangements: list[WorkArrangement] | None = Field(default=None, max_length=service.MAX_ITEMS)
    employment_types: list[EmploymentType] | None = Field(default=None, max_length=service.MAX_ITEMS)
    seniorities: list[Seniority] | None = Field(default=None, max_length=service.MAX_ITEMS)
    salary_floor: SalaryFloor | None = None
    company_names: list[str] | None = Field(default=None, max_length=service.MAX_ITEMS)


class NewConstraint(ConstraintFields):
    """kind is a plain string, not the enum: a kind the matcher cannot enforce is 422 constraint_not_enforceable with a
    suggestion (API.md 3.2), not a field error."""

    kind: str = Field(max_length=40)
    statement: str = Field(max_length=service.MAX_STATEMENT)


class RecordConstraint(ConstraintFields):
    id: UUID
    kind: ConstraintKind
    statement: str
    source: RecordSource
    created_at: datetime


class RecordPreference(BaseModel):
    id: UUID
    kind: PreferenceKind
    value: str
    weight: int
    statement: str
    source: RecordSource
    created_at: datetime
    notice: Literal["not_guaranteed"] | None  # a dealbreaker no job field can check lowers rank and guarantees nothing
    # Only in the answer to a write: the hard constraint a dealbreaker fits, in the shape POST /constraints takes.
    # Nothing is applied until the candidate posts it.
    suggested_constraint: NewConstraint | None = None


class RecordEvent(BaseModel):
    """One row of the change log or the proposal queue. before and after are the row as it was and as it became."""

    id: UUID
    status: ProfileEventType
    entity: ProfileEntity
    operation: ProfileOperation
    entity_id: UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    source: RecordSource | None
    origin: ProposalOrigin | None  # where the proposal came from; an answer names where its proposal came from
    proposal_event_id: UUID | None
    reason: str | None
    record_version_after: int | None
    created_at: datetime


class RecordDocument(BaseModel):
    id: UUID
    original_filename: str | None
    mime_type: str
    byte_size: int
    status: DocumentStatus
    parse_error_code: str | None
    hidden_text_chars: int
    created_at: datetime
    parsed_at: datetime | None


class RecordView(BaseModel):
    """The confirmed record on one page. Facts waiting for review are counted here; GET /v1/me/facts lists them."""

    record_version: int
    facts: list[RecordFact]
    constraints: list[RecordConstraint]
    preferences: list[RecordPreference]
    unconfirmed_facts: int
    open_proposals: int
    documents: list[RecordDocument]


class FactWritten(BaseModel):
    fact: RecordFact
    record_version: int


class ConstraintWritten(BaseModel):
    constraint: RecordConstraint
    record_version: int


class PreferenceWritten(BaseModel):
    preference: RecordPreference
    record_version: int


class VersionOnly(BaseModel):
    record_version: int


class ReplacedBy(BaseModel):
    fact_id: UUID
    status: Literal["confirmed"]
    source: RecordSource


class ReviewResult(BaseModel):
    fact_id: UUID
    status: Literal["confirmed", "rejected", "superseded"]  # a confirmed fact that is rejected is retired: superseded
    replaced_by: ReplacedBy | None = None


class FactsReviewed(BaseModel):
    record_version: int
    results: list[ReviewResult]
    unconfirmed_remaining: int


class ProposalConfirmed(BaseModel):
    entity: ProfileEntity
    entity_id: UUID
    record_version: int


class ProposalDeclined(BaseModel):
    event_id: UUID


# ---- what the API takes --------------------------------------------------------------------------


class NewFact(BaseModel):
    kind: FactKind
    statement: str = Field(max_length=service.MAX_STATEMENT)


class FactDecision(BaseModel):
    fact_id: UUID
    action: Literal["confirm", "correct", "reject"]
    statement: str | None = Field(default=None, max_length=service.MAX_STATEMENT)  # the candidate's words, to correct
    reason: str | None = Field(default=None, max_length=service.MAX_REASON)

    @model_validator(mode="after")
    def _a_correction_has_its_words(self) -> Self:
        if self.action == "correct" and self.statement is None:
            raise ValueError("statement is required to correct a fact")
        return self


class FactReview(BaseModel):
    decisions: list[FactDecision] = Field(min_length=1, max_length=MAX_DECISIONS)


class ConstraintChange(ConstraintFields):
    """PATCH: what is not named stays. A constraint's kind never changes; naming another one is refused."""

    kind: str | None = Field(default=None, max_length=40)
    statement: str | None = Field(default=None, max_length=service.MAX_STATEMENT)


class NewPreference(BaseModel):
    """value defaults to the statement for kind other and untyped_dealbreaker and is required for the others; weight is
    required (-3 to 3, never 0) except for a dealbreaker, which is always -3 (REC-04)."""

    kind: PreferenceKind
    statement: str = Field(max_length=service.MAX_STATEMENT)
    value: str | None = Field(default=None, max_length=service.MAX_VALUE)
    weight: int | None = Field(default=None, ge=-3, le=3)


class PreferenceChange(BaseModel):
    kind: PreferenceKind | None = None
    statement: str | None = Field(default=None, max_length=service.MAX_STATEMENT)
    value: str | None = Field(default=None, max_length=service.MAX_VALUE)
    weight: int | None = Field(default=None, ge=-3, le=3)


def _answer(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """A write's answer as the declared model serializes it: the same timestamp format as the reads, and a shape that
    cannot drift from the contract (a write returns a prepared response, which FastAPI would not check)."""
    return model.model_validate(data).model_dump(mode="json")


# ---- the record ----------------------------------------------------------------------------------


@router.get("/record", response_model=Envelope[RecordView])
def get_record(principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        record = api.get_record(conn, principal.user_id)
        record["documents"] = service.record_documents(conn, principal.user_id)
    return envelope(record)


# ---- facts ---------------------------------------------------------------------------------------


@router.get("/facts", response_model=Envelope[list[RecordFact]])
def list_facts(
    principal: Signed, status: FactStatus | None = None, limit: PageSize = 20, cursor: After = None
) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        facts, next_cursor = service.list_facts(conn, principal.user_id, status=status, limit=limit, cursor=cursor)
    return envelope(facts, next_cursor=next_cursor, limit=limit)


@router.post("/facts", status_code=201, response_model=Envelope[FactWritten])
def add_fact(body: NewFact, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        applied = service.add_fact(conn, write.user_id, body.kind, body.statement)
        return _answer(FactWritten, {"fact": applied.row, "record_version": applied.record_version})

    return write.run(effect, status=201)


def _decide(conn: Connection, user_id: UUID, decision: FactDecision) -> tuple[dict[str, Any], int]:
    """One decision of a review: the result the candidate sees and the version of the record after it."""
    if decision.action == "confirm":
        applied = service.confirm_fact(conn, user_id, decision.fact_id)
        return {"fact_id": decision.fact_id, "status": "confirmed"}, applied.record_version
    if decision.action == "reject":
        applied = service.reject_fact(conn, user_id, decision.fact_id, reason=decision.reason)
        return {"fact_id": decision.fact_id, "status": applied.row["status"]}, applied.record_version
    applied = service.correct_fact(conn, user_id, decision.fact_id, str(decision.statement), reason=decision.reason)
    replaced_by = {"fact_id": applied.row["id"], "status": "confirmed", "source": applied.row["source"]}
    return {"fact_id": decision.fact_id, "status": "superseded", "replaced_by": replaced_by}, applied.record_version


@router.post("/facts/review", response_model=Envelope[FactsReviewed])
def review_facts(body: FactReview, write: Idempotent) -> Response:
    """Confirm, correct or reject one or many facts in one write: all of them happen, or none does."""
    ids = [decision.fact_id for decision in body.decisions]
    if len(set(ids)) != len(ids):
        raise InvalidField("decisions", "duplicate")

    def effect(conn: Connection) -> dict[str, Any]:
        outcomes = [_decide(conn, write.user_id, decision) for decision in body.decisions]
        remaining = int(service.record_header(conn, write.user_id).unconfirmed_facts)
        results = [result for result, _ in outcomes]
        return _answer(
            FactsReviewed,
            {"record_version": outcomes[-1][1], "results": results, "unconfirmed_remaining": remaining},
        )

    return write.run(effect)


# ---- hard constraints ----------------------------------------------------------------------------


def _typed(body: ConstraintFields) -> dict[str, Any]:
    return body.model_dump(exclude_none=True, exclude={"kind", "statement"})


@router.get("/constraints", response_model=Envelope[list[RecordConstraint]])
def list_constraints(principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        return envelope(service.list_constraints(conn, principal.user_id))


@router.post("/constraints", status_code=201, response_model=Envelope[ConstraintWritten])
def add_constraint(body: NewConstraint, write: Idempotent) -> Response:
    """Make this the person's hard constraint of its kind. There is one active constraint per kind, so this replaces
    the one there is (the change log keeps it): to keep part of it, PATCH it."""

    def effect(conn: Connection) -> dict[str, Any]:
        applied = service.set_constraint(conn, write.user_id, body.kind, _typed(body), body.statement)
        return _answer(ConstraintWritten, {"constraint": applied.row, "record_version": applied.record_version})

    return write.run(effect, status=201)


@router.patch("/constraints/{constraint_id}", response_model=Envelope[ConstraintWritten])
def change_constraint(constraint_id: UUID, body: ConstraintChange, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        applied = service.change_constraint(conn, write.user_id, constraint_id, changes)
        return _answer(ConstraintWritten, {"constraint": applied.row, "record_version": applied.record_version})

    return write.run(effect)


@router.delete("/constraints/{constraint_id}", response_model=Envelope[VersionOnly])
def remove_constraint(constraint_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        removed = service.remove_constraint(conn, write.user_id, constraint_id)
        return _answer(VersionOnly, {"record_version": removed.record_version})

    return write.run(effect)


# ---- soft preferences ----------------------------------------------------------------------------


@router.get("/preferences", response_model=Envelope[list[RecordPreference]])
def list_preferences(principal: Signed, limit: PageSize = 20, cursor: After = None) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        preferences, next_cursor = service.list_preferences(conn, principal.user_id, limit=limit, cursor=cursor)
    return envelope(preferences, next_cursor=next_cursor, limit=limit)


@router.post("/preferences", status_code=201, response_model=Envelope[PreferenceWritten])
def add_preference(body: NewPreference, write: Idempotent) -> Response:
    """A soft preference. A dealbreaker is stored as untyped_dealbreaker with notice not_guaranteed; when it fits a hard
    constraint kind the answer also carries suggested_constraint, which the candidate may post to /constraints."""

    def effect(conn: Connection) -> dict[str, Any]:
        applied = service.add_preference(
            conn, write.user_id, body.kind, body.statement, value=body.value, weight=body.weight
        )
        return _answer(PreferenceWritten, {"preference": applied.row, "record_version": applied.record_version})

    return write.run(effect, status=201)


@router.patch("/preferences/{preference_id}", response_model=Envelope[PreferenceWritten])
def change_preference(preference_id: UUID, body: PreferenceChange, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        applied = service.change_preference(conn, write.user_id, preference_id, body.model_dump(exclude_none=True))
        return _answer(PreferenceWritten, {"preference": applied.row, "record_version": applied.record_version})

    return write.run(effect)


@router.delete("/preferences/{preference_id}", response_model=Envelope[VersionOnly])
def remove_preference(preference_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        removed = service.remove_preference(conn, write.user_id, preference_id)
        return _answer(VersionOnly, {"record_version": removed.record_version})

    return write.run(effect)


# ---- the change log and proposals ----------------------------------------------------------------


@router.get("/events", response_model=Envelope[list[RecordEvent]])
def list_events(
    principal: Signed, status: ProfileEventType | None = None, limit: PageSize = 20, cursor: After = None
) -> dict[str, Any]:
    """The change log, newest first: applied, declined and expired. ?status=proposed lists proposals still waiting."""
    with db.transaction(principal.user_id) as conn:
        found, next_cursor = events.list_events(conn, principal.user_id, status=status, limit=limit, cursor=cursor)
    return envelope(found, next_cursor=next_cursor, limit=limit)


@router.post("/events/{event_id}/confirm", response_model=Envelope[ProposalConfirmed])
def confirm_proposal(event_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        applied = events.confirm(conn, write.user_id, event_id)
        shown = {"entity": applied.entity, "entity_id": applied.row["id"], "record_version": applied.record_version}
        return _answer(ProposalConfirmed, shown)

    return write.run(effect)


@router.post("/events/{event_id}/decline", response_model=Envelope[ProposalDeclined])
def decline_proposal(event_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> dict[str, Any]:
        return _answer(ProposalDeclined, {"event_id": events.decline(conn, write.user_id, event_id)})

    return write.run(effect)
