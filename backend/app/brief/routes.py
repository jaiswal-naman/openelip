"""HTTP routes of the brief module: GET /v1/briefs, GET /v1/briefs/{id} and POST /v1/briefs/unsubscribe (API.md 2.7).

The routes and their Pydantic models are the contract: the web types are generated from them. The person is the
session's, never anything in a path or body, and another person's brief is a 404 because the query filters by the
session's user id and row-level security fences it again. The unsubscribe route is the exception that proves the
rule: it has no session and no Idempotency-Key (API.md 1.6); the signed token in its query is the person.
"""

from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict

from app.platform import db, log
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.sessions import Principal, current_user
from app.trust import api as trust

from . import unsubscribe, views
from .models import BriefStatus

router = APIRouter()

# Third-party text (a job's title and company): clients show it as text and never as markup (API.md 1.10).
UNTRUSTED: JsonDict = {"x-untrusted-text": True}

Signed = Annotated[Principal, Depends(current_user)]
PageSize = Annotated[int, Query(ge=1, le=100)]
After = Annotated[str | None, Query(max_length=400)]  # the opaque cursor from meta.next_cursor


class BriefDelivery(BaseModel):
    """in_app is the brief's own status. email is null when the person has not agreed to the email; otherwise what the
    outbox row says: queued, sent, or failed (never sent: cancelled, expired or refused)."""

    in_app: BriefStatus
    email: Literal["queued", "sent", "failed"] | None
    email_sent_at: datetime | None


class BriefListItem(BaseModel):
    id: UUID
    local_date: date
    status: BriefStatus
    item_count: int
    delivery: BriefDelivery


class BriefAction(BaseModel):
    """An ordinary endpoint to call. A dismissal also needs a reason, which the person chooses when they dismiss."""

    action: Literal["open", "save", "dismiss", "review"]
    method: Literal["GET", "POST"]
    path: str
    body: dict[str, str] | None = None


class BriefIntroduction(BaseModel):
    """An employer asking whether the person would like to talk about a role. The person answers on Today; nothing about
    what they would share is here."""

    kind: Literal["introduction"]
    introduction_id: UUID
    organization_name: str = Field(json_schema_extra=UNTRUSTED)
    role_title: str = Field(json_schema_extra=UNTRUSTED)
    expires_at: datetime


class BriefDecision(BaseModel):
    """One thing waiting for the person: a match, or the record changes someone suggested. A match nobody assessed
    (is_assessed false) has no requirement table and no verdict: the search ranked it, no model has read it."""

    kind: Literal["match", "record_proposals"]
    title: str = Field(json_schema_extra=UNTRUSTED)
    why: str
    ref: dict[str, Any]
    actions: list[BriefAction]
    is_assessed: bool
    change_note: str | None  # a match shown again because the posting changed: what changed
    note: str | None  # for example that the apply link could not be re-checked this morning


class BriefDone(BaseModel):
    at: datetime
    text: str
    ref: dict[str, UUID]


class BriefDetail(BaseModel):
    id: UUID
    local_date: date
    status: BriefStatus
    delivery: BriefDelivery
    decisions: list[Annotated[BriefIntroduction | BriefDecision, Field(discriminator="kind")]]
    done_since_last_brief: list[BriefDone]
    open_record_proposals: int


class Unsubscribed(BaseModel):
    is_brief_email_on: bool


@router.get("/v1/briefs", response_model=Envelope[list[BriefListItem]])
def list_my_briefs(principal: Signed, limit: PageSize = 20, cursor: After = None) -> dict[str, Any]:
    """Past briefs, newest first: the in-app inbox. A day with nothing to say is listed as suppressed."""
    with db.transaction(principal.user_id) as conn:
        briefs, next_cursor = views.list_briefs(conn, principal.user_id, limit=limit, cursor=cursor)
    return envelope(briefs, next_cursor=next_cursor, limit=limit)


@router.get("/v1/briefs/{brief_id}", response_model=Envelope[BriefDetail])
def read_brief(brief_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        brief = views.brief_view(conn, brief_id, principal.user_id)
    if brief is None:
        raise ApiError(404, "not_found", "No such resource.")
    return envelope(brief)


@router.post("/v1/briefs/unsubscribe", response_model=Envelope[Unsubscribed])
def unsubscribe_one_click(
    t: Annotated[str, Query(max_length=200)],
    one_click: Annotated[str, Form(alias="List-Unsubscribe")],
) -> dict[str, Any]:
    """RFC 8058 one-click unsubscribe: withdraws the brief_email consent of the person the signed token names. No
    session, no cookie, no redirect; asking again changes nothing. A GET to this path does nothing at all."""
    if one_click != "One-Click":
        raise ApiError(
            422,
            "validation_failed",
            "One or more fields are invalid.",
            {"fields": [{"field": "List-Unsubscribe", "issue": "must be One-Click"}]},
        )
    user_id = unsubscribe.verify(t, now=datetime.now(timezone.utc))
    if user_id is None:
        raise ApiError(404, "not_found", "No such resource.")  # a forged, altered or expired link says nothing more
    with db.transaction(user_id) as conn:
        changed = trust.withdraw_consent(conn, user_id, "brief_email") > 0
        if changed:
            trust.audit_log(conn, "brief.unsubscribed", "candidate", user_id, {"channel": "email"})
    log.event("brief.unsubscribe", outcome="withdrawn" if changed else "already_off")
    return envelope(Unsubscribed(is_brief_email_on=False))
