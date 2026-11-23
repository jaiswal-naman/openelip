"""The introduction routes, /v1/introductions, /v1/roles/{id}/shortlist and /v1/orgs/{id}/answer-clock: list, get,
accept, decline, answer, shortlist and the answer clock (HIR-04 to HIR-06).

The person is always the session's. A candidate reaches their own asks; an employer member reaches accepted
introductions of their own organization; everything else is 404 (API.md 1.11). Writes need an Idempotency-Key.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import answers, introductions
from .answers import MAX_REASON

router = APIRouter()

Signed = Annotated[Principal, Depends(current_user)]
PageSize = Annotated[int, Query(ge=1, le=100)]
After = Annotated[str | None, Query(max_length=400)]
Status = Literal["asked", "accepted", "declined", "expired", "withdrawn"]


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share_preview_hash: str
    consent_text_version: str


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: Literal["yes", "no"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REASON)]


class Resolved(BaseModel):
    id: UUID
    status: str
    responded_at: datetime | None = None
    answer_due_at: datetime | None = None


class IntroductionAccepted(BaseModel):
    introduction: Resolved
    shared: str


class Declined(BaseModel):
    introduction: Resolved


class Answered(BaseModel):
    introduction: dict[str, Any]
    candidate_sees: str
    email: str | None


@router.get("/v1/introductions", response_model=Envelope[list[dict[str, Any]]])
def list_introductions(
    principal: Signed,
    role_id: UUID | None = None,
    org_id: UUID | None = None,
    status: Status | None = None,
    limit: PageSize = 20,
    cursor: After = None,
) -> dict[str, Any]:
    """Candidate: their own introductions. Employer (`role_id` or `org_id`): the accepted ones of that organization."""
    with db.transaction(principal.user_id) as conn:
        if role_id is None and org_id is None:
            items, next_cursor = introductions.list_for_candidate(conn, principal.user_id, limit=limit, cursor=cursor)
        else:
            items, next_cursor = introductions.list_for_employer(
                conn, principal.user_id, role_id=role_id, org_id=org_id, status=status, limit=limit, cursor=cursor
            )
    return envelope(items, next_cursor=next_cursor, limit=limit)


@router.get("/v1/introductions/{introduction_id}", response_model=Envelope[dict[str, Any]])
def read_introduction(introduction_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        view = introductions.get(conn, principal.user_id, introduction_id)
    view.pop("view")
    return envelope(view)


@router.post("/v1/introductions/{introduction_id}/accept", response_model=Envelope[IntroductionAccepted])
def accept_introduction(introduction_id: UUID, body: AcceptBody, write: Annotated[Write, Depends(idempotent)]) -> Any:
    return write.run(
        lambda conn: introductions.accept(
            conn, write.user_id, introduction_id, body.share_preview_hash, body.consent_text_version
        )
    )


@router.post("/v1/introductions/{introduction_id}/decline", response_model=Envelope[Declined])
def decline_introduction(introduction_id: UUID, write: Annotated[Write, Depends(idempotent)]) -> Any:
    return write.run(lambda conn: introductions.decline(conn, write.user_id, introduction_id))


@router.post("/v1/introductions/{introduction_id}/answer", response_model=Envelope[Answered])
def answer_introduction(introduction_id: UUID, body: AnswerBody, write: Annotated[Write, Depends(idempotent)]) -> Any:
    return write.run(lambda conn: answers.answer(conn, write.user_id, introduction_id, body.answer, body.reason))


@router.get("/v1/roles/{role_id}/shortlist", response_model=Envelope[list[dict[str, Any]]])
def read_shortlist(role_id: UUID, principal: Signed, limit: PageSize = 20, cursor: After = None) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        items, next_cursor = answers.shortlist(conn, principal.user_id, role_id, limit=limit, cursor=cursor)
    return envelope(items, next_cursor=next_cursor, limit=limit)


@router.get("/v1/orgs/{org_id}/answer-clock", response_model=Envelope[dict[str, Any]])
def read_answer_clock(org_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        clock = answers.answer_clock(conn, principal.user_id, org_id)
    return envelope(clock)
