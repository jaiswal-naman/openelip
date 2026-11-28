"""The campaign read routes: GET /v1/campaigns and GET /v1/campaigns/{id} (API 3.8; CMP-01 to CMP-05, GRD-04, GRD-07).

Plain reads on the caller's own API connection, so row-level security holds as well as the user filter in every query.
Another person's campaign is a 404. The write routes are in routes.py. Model-written text (a dossier claim, a prep
question, a draft's subject and body) is marked untrusted for clients, which show it as text.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.sessions import Principal, current_user

from . import api

router = APIRouter()
UNTRUSTED: JsonDict = {"x-untrusted-text": True}
Signed = Annotated[Principal, Depends(current_user)]

Stage = Literal["researching", "needs_you", "applied", "talking", "interview", "offer", "closed"]


class NextStep(BaseModel):
    step_key: str
    agent_role: str
    text: str


class Card(BaseModel):
    id: UUID
    title: str = Field(json_schema_extra=UNTRUSTED)
    company_name: str = Field(json_schema_extra=UNTRUSTED)
    from_introduction: bool
    paused_at: datetime | None
    pause_reason: Literal["candidate", "budget_exhausted", "scam_suspected"] | None
    closed_reason: str | None
    next_step: NextStep | None
    pending_approvals: int
    last_activity_at: datetime | None


class Column(BaseModel):
    stage: Stage
    campaigns: list[Card]
    total: int | None = None  # the closed column only


class Board(BaseModel):
    columns: list[Column]


class JobRef(BaseModel):
    id: int
    title: str = Field(json_schema_extra=UNTRUSTED)
    company_name: str = Field(json_schema_extra=UNTRUSTED)
    apply_url: str | None = Field(json_schema_extra=UNTRUSTED)


class IntroductionRef(BaseModel):
    id: UUID
    employer_answer: str | None
    answer_due_at: datetime | None


class ScamSignal(BaseModel):
    kind: str
    quote: str = Field(json_schema_extra=UNTRUSTED)  # a sentence of the posting


class Step(BaseModel):
    step_key: str
    agent_role: str
    status: str
    due_at: datetime | None
    error_code: str | None
    todo: str | None
    text: str


class Dossier(BaseModel):
    id: UUID
    version: int
    incomplete: bool
    claims: list[dict[str, Any]] = Field(
        json_schema_extra=UNTRUSTED
    )  # {topic?, text, sources: [{url, quote, retrieved_at}]}
    missing_topics: list[str]


class Packet(BaseModel):
    id: UUID
    status: str
    approval_id: UUID | None
    action_status: str | None


class FollowUp(BaseModel):
    step_key: str | None
    action_id: int
    approval_id: UUID | None
    status: str
    delivery: str | None  # the outbox state: pending, sending, sent, failed, cancelled
    to: str | None
    subject: str | None = Field(json_schema_extra=UNTRUSTED)
    body: str | None = Field(json_schema_extra=UNTRUSTED)
    undo_until: datetime | None
    outcome_code: str | None
    outcome_text: str | None


class PendingApproval(BaseModel):
    id: UUID
    kind: str


class CampaignDetail(BaseModel):
    id: UUID
    job: JobRef
    stage: Stage
    from_introduction: bool
    introduction: IntroductionRef | None
    paused_at: datetime | None
    pause_reason: Literal["candidate", "budget_exhausted", "scam_suspected"] | None
    pause_text: str | None
    scam_signals: list[ScamSignal]
    closed_at: datetime | None
    closed_reason: str | None
    budget_usd: str
    spent_usd: str
    follow_up_after_days: int
    follow_up_email: str | None
    created_at: datetime
    server_time: datetime
    next_step: NextStep | None
    steps: list[Step]
    dossier: Dossier | None
    fit: dict[str, Any] | None
    packet: Packet | None
    prep: dict[str, Any] | None = Field(json_schema_extra=UNTRUSTED)
    follow_ups: list[FollowUp]
    pending_approvals: list[PendingApproval]


@router.get("/v1/campaigns", response_model=Envelope[Board])
def read_board(principal: Signed, include_closed: Annotated[bool, Query()] = False) -> dict[str, Any]:
    """One column per stage (the closed one only when asked); each card has its pause, next step and approvals."""
    with db.transaction(principal.user_id) as conn:
        found = api.board(conn, principal.user_id, include_closed=include_closed)
    return envelope(found)


@router.get("/v1/campaigns/{campaign_id}", response_model=Envelope[CampaignDetail])
def read_campaign(campaign_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        found = api.detail(conn, principal.user_id, campaign_id)
    if found is None:
        raise ApiError(404, "not_found", "No such resource.")
    return envelope(found)
