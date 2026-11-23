"""The campaign write routes (API.md 2.11, 3.7; CMP-01, CMP-04, CMP-07, GRD-07):

    POST  /v1/campaigns                       start a campaign for a job the person approved
    PATCH /v1/campaigns/{campaign_id}         the model budget, the quiet days before a follow-up, the follow-up address
    POST  /v1/campaigns/{campaign_id}/pause   stop new steps and hold a follow-up in its undo window
    POST  /v1/campaigns/{campaign_id}/resume  start again (paused at its budget: raise the budget first)
    POST  /v1/campaigns/{campaign_id}/cancel  close it, expiring what waits for the person

The reads (`GET /v1/campaigns`, `GET /v1/campaigns/{id}`) are plan 07-06's. Everything here runs on the API login: the
person is the session's, another person's campaign is a 404 (row-level security), and every write takes an
Idempotency-Key. No route here opens a worker or match transaction.
"""

import re
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection

from app.platform.idempotency import Write, idempotent

from . import service

router = APIRouter()

Idempotent = Annotated[Write, Depends(idempotent)]

MAX_JOB_ID = 2**63 - 1  # job ids are bigints
ADDRESS_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # the database CHECK's pattern
MAX_ADDRESS = 254
MAX_NOTE = 500  # assumption: characters in a cancel note


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: int = Field(ge=1, le=MAX_JOB_ID)
    budget_usd: Decimal | None = None  # US dollars as text, "1.50"; the instance default when left out

    @field_validator("budget_usd", mode="before")
    @classmethod
    def _text_or_number(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("send the budget as text like 1.50")
        return value


class PatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_usd: Decimal | None = None
    follow_up_after_days: int | None = Field(default=None, ge=1, le=60)
    follow_up_email: str | None = None  # null clears the address

    @field_validator("follow_up_email")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if len(trimmed) > MAX_ADDRESS or not ADDRESS_PATTERN.fullmatch(trimmed):
            raise ValueError("not an email address")
        return trimmed


class EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=MAX_NOTE)
    reason: str | None = Field(default=None, pattern="^(cancelled|offer_accepted|offer_declined)$")


@router.post("/v1/campaigns", status_code=201)
def start_campaign(body: StartIn, write: Idempotent) -> Response:
    """Approve a match: start a campaign at `researching`. A second start for the job is 409 with the open campaign's
    id, a closed job 410, a job that breaks a hard constraint 422, no AI-processing consent 403."""

    def effect(conn: Connection) -> dict[str, Any]:
        budget = None if body.budget_usd is None else service.budget_microusd(body.budget_usd)
        created = service.start(conn, write.user_id, body.job_id, budget_microusd=budget)
        return service.summary(conn, created)

    return write.run(effect, status=201)


@router.patch("/v1/campaigns/{campaign_id}")
def change_campaign(campaign_id: UUID, body: PatchIn, write: Idempotent) -> Response:
    """Change the budget, `follow_up_after_days` or `follow_up_email`. A closed campaign is 409."""

    def effect(conn: Connection) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if body.budget_usd is not None:
            changes["budget_microusd"] = service.budget_microusd(body.budget_usd)
        if body.follow_up_after_days is not None:
            changes["follow_up_after_days"] = body.follow_up_after_days
        if "follow_up_email" in body.model_fields_set:
            changes["follow_up_email"] = body.follow_up_email
        return service.update(conn, write.user_id, campaign_id, **changes)

    return write.run(effect)


@router.post("/v1/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: UUID, write: Idempotent, body: EmptyIn | None = None) -> Response:
    return write.run(lambda conn: service.pause(conn, write.user_id, campaign_id))


@router.post("/v1/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: UUID, write: Idempotent, body: EmptyIn | None = None) -> Response:
    """Resume. A campaign paused at its budget is 409 `budget_exhausted` until `PATCH` raises the budget."""
    return write.run(lambda conn: service.resume(conn, write.user_id, campaign_id))


@router.post("/v1/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: UUID, write: Idempotent, body: CancelIn | None = None) -> Response:
    """Cancel. `reason` offer_accepted or offer_declined only at the offer stage."""
    given = body or CancelIn()
    return write.run(
        lambda conn: service.cancel(conn, write.user_id, campaign_id, note=given.note, reason=given.reason)
    )
