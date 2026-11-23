"""HTTP routes of the match module: today's matches, the decision ledger and feedback (API.md 2.5 and 2.6; plan 04-04).

    GET  /v1/matches                          the matches shown on a date (default: the caller's local today)
    GET  /v1/matches/{decision_id}            one match with the posting text and its constraint checks
    POST /v1/matches/{decision_id}/feedback   save, unsave or dismiss with a reason (a reason may propose a change)
    GET  /v1/decisions                        the ledger: withheld and filtered jobs with their reasons ("why not")
    GET  /v1/decisions/{decision_id}          one decision with its evidence, its constraint or its gaps

The models are the contract: the web types are generated from them. A route translates HTTP to one call into the ledger
or into feedback. The person is the session's, never a field of a path or a body: someone else's decision is a 404
because every query names the session's person. Posting text and resume text are third-party text and are marked
x-untrusted-text in the schema (API.md 1.10): the web shows them as plain text.
"""

from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import JsonDict
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import feedback, ledger
from .models import (
    MATCH_OUTCOMES,
    MATCH_REASONS,
    ConstraintKind,
    DismissReason,
    EvidenceVerdict,
    FeedbackKind,
    MatchOutcome,
    MatchReason,
    UnverifiedFlag,
)

router = APIRouter()

MAX_JOB_ID = 2**63 - 1  # job ids are bigints: a larger number cannot be one
MAX_COMMENT = 500  # the database's own limit on feedback_events.comment
UNTRUSTED: JsonDict = {"x-untrusted-text": True}

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]
PageSize = Annotated[int, Query(ge=1, le=100)]
After = Annotated[str | None, Query(max_length=400)]  # the opaque cursor from meta.next_cursor
Importance = Literal["required", "preferred"]


# ---- what the API returns: the job ---------------------------------------------------------------------------------


class CharSpan(BaseModel):
    start: int  # Unicode code points, end exclusive
    end: int


class PayRange(BaseModel):
    min: int | None
    max: int | None
    currency: str  # ISO 4217; annual amounts in whole currency units (API.md 1.4)


class JobBoard(BaseModel):
    ats: Literal["greenhouse", "lever", "ashby", "direct"]
    board: str  # the board's token at that ATS


class JobBrief(BaseModel):
    """The job as a ledger row shows it."""

    id: int
    title: str
    company_name: str
    pay: PayRange | None
    status: Literal["open", "closed"]
    last_verified_at: datetime


class MatchJob(JobBrief):
    """The job as a match shows it: the same fields as GET /v1/jobs/{id}, without its text."""

    location_raw: str | None
    country: str | None  # ISO 3166-1 alpha-2; null when the posting's place is not recognised
    city: str | None
    work_arrangements: list[Literal["onsite", "hybrid", "remote"]]  # empty: the posting does not say
    is_remote: bool | None  # true when work_arrangements has remote, null when it is empty
    employment_type: Literal["full_time", "part_time", "contract", "internship", "temporary", "unknown"]
    apply_url: str
    source: JobBoard
    posted_at: datetime | None
    content_version: int


# ---- what the API returns: the evidence table ----------------------------------------------------------------------


class JobCitation(BaseModel):
    """Where in the posting the requirement is. The quote is the stored text at the stored span, never generated."""

    span: CharSpan
    quote: str = Field(json_schema_extra=UNTRUSTED)


class RequirementRef(BaseModel):
    id: int
    text: str = Field(json_schema_extra=UNTRUSTED)  # a quote from the posting
    importance: Importance
    job_citation: JobCitation


class RecordSource(BaseModel):
    document_id: UUID
    span: CharSpan  # into the document's extracted text


class RecordCitation(BaseModel):
    """A fact of the Career Record a verdict rests on. A fact the person has corrected or retired since still shows what
    it said when the verdict was made, with is_current false. source is null for a fact the person wrote herself."""

    fact_id: UUID
    quote: str = Field(json_schema_extra=UNTRUSTED)
    source: RecordSource | None
    is_current: bool


class EvidenceItem(BaseModel):
    requirement: RequirementRef
    verdict: EvidenceVerdict
    record_citations: list[RecordCitation]  # empty exactly when the verdict is no_evidence


# ---- what the API returns: matches ---------------------------------------------------------------------------------


class MatchFeedback(BaseModel):
    is_saved: bool
    is_dismissed: bool


class MatchView(BaseModel):
    decision_id: UUID
    rank_position: int
    reason_codes: list[MatchReason]
    change_note: str | None  # a job shown again because it changed says what changed (MAT-07)
    summary: str = Field(json_schema_extra=UNTRUSTED)
    unverified: list[UnverifiedFlag]  # constrained fields the posting does not state: shown as "not stated"
    job: MatchJob
    evidence: list[EvidenceItem]  # every requirement of the job, once
    feedback: MatchFeedback


class ConstraintCheck(BaseModel):
    kind: ConstraintKind
    statement: str
    state: Literal["satisfied", "not_stated"]


class MatchDetail(MatchView):
    description: str = Field(json_schema_extra=UNTRUSTED)  # the posting's plain text, which the spans point into
    constraint_checks: list[ConstraintCheck]


class RunSummary(BaseModel):
    id: UUID
    record_version: int
    shown: int
    withheld: int
    filtered: int  # every job the constraints removed, stored or not
    checker: Literal["passed", "disagreed"]  # whether the independent constraint checker agreed with the filter


class TodayMatches(BaseModel):
    local_date: date
    timezone: str
    run: RunSummary | None  # the latest run that finished on the date; null when none did
    matches: list[MatchView]  # zero to five; zero is a valid day


# ---- what the API returns: decisions -------------------------------------------------------------------------------


class ConstraintRef(BaseModel):
    """The constraint that removed a job. id and statement are null when the person has since removed it."""

    id: UUID | None
    kind: ConstraintKind
    statement: str | None


class Gap(BaseModel):
    requirement: str = Field(json_schema_extra=UNTRUSTED)
    importance: Importance
    verdict: EvidenceVerdict


class DecisionView(BaseModel):
    decision_id: UUID | None  # null for an on-request check, which stores nothing
    local_date: date
    outcome: MatchOutcome | Literal["not_evaluated"]  # not_evaluated only in the answer to an on-request check
    reason_codes: list[MatchReason]
    constraint: ConstraintRef | None
    gaps: list[Gap]
    evaluated: Literal["on_request"] | None = None
    job: JobBrief


class DecisionDetail(DecisionView):
    violated_kinds: list[ConstraintKind]
    unverified: list[UnverifiedFlag]
    rank_position: int | None
    summary: str | None = Field(default=None, json_schema_extra=UNTRUSTED)
    change_note: str | None
    evidence: list[EvidenceItem]


# ---- what the API takes --------------------------------------------------------------------------------------------


class FeedbackBody(BaseModel):
    kind: FeedbackKind
    reason: DismissReason | None = None
    comment: str | None = Field(default=None, max_length=MAX_COMMENT)

    @field_validator("comment")
    @classmethod
    def _text_the_database_can_hold(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:  # PostgreSQL text cannot hold NUL
            raise ValueError("a comment cannot hold a NUL character")
        return value

    @model_validator(mode="after")
    def _a_dismissal_has_its_reason_and_nothing_else_has_one(self) -> Self:
        if (self.kind == "dismiss") != (self.reason is not None):
            raise ValueError("a dismissal needs a reason, and only a dismissal has one")
        return self


class FeedbackRecorded(BaseModel):
    is_saved: bool
    is_dismissed: bool
    proposal_event_id: UUID | None  # a record change waiting for the person: POST /v1/me/events/{id}/confirm applies it


def _answer(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """A write's answer as the declared model serializes it, so it cannot drift from the contract."""
    return model.model_validate(data).model_dump(mode="json")


def _one_of(values: str, allowed: tuple[str, ...], name: str) -> list[str]:
    """A comma list of values of a closed vocabulary; one that is not in it is a 400 (API.md 1.11)."""
    given = [value.strip() for value in values.split(",") if value.strip()]
    if any(value not in allowed for value in given):
        raise ApiError(400, "invalid_request", f"{name} holds a value that is not one of: {', '.join(allowed)}.")
    return given


# ---- today's matches -----------------------------------------------------------------------------------------------


@router.get("/v1/matches", response_model=Envelope[TodayMatches])
def todays_matches(principal: Signed, day: Annotated[date | None, Query(alias="date")] = None) -> dict[str, Any]:
    """The matches shown on a date, ranked, each with the requirement evidence table. Zero is a valid day."""
    with db.transaction(principal.user_id) as conn:
        zone = ledger.timezone_of(conn, principal.user_id)
        local_date = day or ledger.today_in(zone)
        data = {
            "local_date": local_date,
            "timezone": zone,
            "run": ledger.run_summary(conn, principal.user_id, local_date),
            "matches": ledger.shown_matches(conn, principal.user_id, local_date),
        }
    return envelope(data)


@router.get("/v1/matches/{decision_id}", response_model=Envelope[MatchDetail])
def one_match(decision_id: UUID, principal: Signed) -> dict[str, Any]:
    """One shown match with the posting's text (for the highlights) and a check of each hard constraint."""
    with db.transaction(principal.user_id) as conn:
        found = ledger.match_by_id(conn, principal.user_id, decision_id)
    if found is None:
        raise ledger.not_found()
    return envelope(found)


@router.post("/v1/matches/{decision_id}/feedback", status_code=201, response_model=Envelope[FeedbackRecorded])
def give_feedback(decision_id: UUID, body: FeedbackBody, write: Idempotent) -> Response:
    """Save, unsave or dismiss a match. A dismissal reason may propose a record change; it changes nothing until the
    person confirms it through /v1/me/events."""

    def effect(conn: Connection) -> dict[str, Any]:
        result = feedback.record_feedback(conn, write.user_id, decision_id, body.kind, body.reason, body.comment)
        return _answer(FeedbackRecorded, asdict(result))

    return write.run(effect, status=201)


# ---- the decision ledger -------------------------------------------------------------------------------------------


@router.get("/v1/decisions", response_model=Envelope[list[DecisionView]])
def decision_ledger(
    principal: Signed,
    outcome: Annotated[str | None, Query(max_length=100)] = None,
    reason: Annotated[str | None, Query(max_length=40)] = None,
    since: Annotated[date | None, Query(alias="from")] = None,
    until: Annotated[date | None, Query(alias="to")] = None,
    job_id: Annotated[int | None, Query(ge=1, le=MAX_JOB_ID)] = None,
    limit: PageSize = 20,
    cursor: After = None,
) -> dict[str, Any]:
    """The decision ledger, newest first. ?job_id= returns the stored decision for one job, or checks that job against
    the caller's hard constraints on the spot (no model call, nothing stored) and returns filtered or not_evaluated."""
    outcomes = _one_of(outcome, MATCH_OUTCOMES, "outcome") if outcome else None
    if reason is not None and reason not in MATCH_REASONS:
        raise ApiError(400, "invalid_request", f"reason is not one of: {', '.join(MATCH_REASONS)}.")
    with db.transaction(principal.user_id) as conn:
        if job_id is not None:
            zone = ledger.timezone_of(conn, principal.user_id)
            found = ledger.job_decision(conn, principal.user_id, job_id, ledger.today_in(zone))
            if found is None:
                raise ledger.not_found()
            return envelope([found], limit=limit, next_cursor=None)
        rows, next_cursor = ledger.decisions_page(
            conn,
            principal.user_id,
            outcomes=outcomes,
            reason=reason,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
        )
    return envelope(rows, limit=limit, next_cursor=next_cursor)


@router.get("/v1/decisions/{decision_id}", response_model=Envelope[DecisionDetail])
def one_decision(decision_id: UUID, principal: Signed) -> dict[str, Any]:
    """One decision of any outcome, with its evidence, the constraint that filtered it or the gaps that withheld it."""
    with db.transaction(principal.user_id) as conn:
        found = ledger.decision_by_id(conn, principal.user_id, decision_id)
    if found is None:
        raise ledger.not_found()
    return envelope(found)
