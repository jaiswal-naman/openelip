"""The signal routes, /v1/signals: send and list the weekly serious-interest signals (HIR-08). A write needs an
Idempotency-Key; the side (candidate or organization) is the caller's, never in the body."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import signals

router = APIRouter()


class SignalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    introduction_id: UUID


@router.post("/v1/signals", status_code=201, response_model=Envelope[dict[str, Any]])
def send_signal(body: SignalBody, write: Annotated[Write, Depends(idempotent)]) -> Any:
    return write.run(lambda conn: signals.send(conn, write.user_id, body.introduction_id), status=201)


@router.get("/v1/signals", response_model=Envelope[dict[str, Any]])
def read_signals(principal: Annotated[Principal, Depends(current_user)], org_id: UUID | None = None) -> dict[str, Any]:
    """The signals the caller sent and received and the weekly quota: a candidate's own, or with `org_id` a member's
    organization (candidate signals appear only after the candidate accepted)."""
    with db.transaction(principal.user_id) as conn:
        return envelope(signals.list_with_quota(conn, principal.user_id, org_id))
