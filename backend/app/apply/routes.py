"""HTTP routes of the apply module: the application tracker (API.md 2.10; APP-01).

    GET  /v1/applications                        the person's applications, each with its status history and times
    POST /v1/applications                        record that the person applied to a job
    POST /v1/applications/{application_id}/status  record a new status with the time it happened

Plan 05-05 adds the packet routes under /v1/applications/packets to this router. The person is the session's, never a
field of a path or a body: someone else's application, packet or job id is a 404 because every query names the session's
person. Writes take an Idempotency-Key (API.md 1.6).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, BeforeValidator, Field
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import tracker
from .models import ApplicationStatus
from .tracker import ApplicationView

router = APIRouter()

MAX_JOB_ID = 2**63 - 1  # job ids are bigints: a larger number cannot be one

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


def _time_as_text(value: Any) -> Any:
    """A time is sent as ISO 8601 text with a zone. Pydantic would also read a bare number as a Unix timestamp, which no
    client means to send."""
    if not isinstance(value, str):
        raise ValueError("send the time as text with a zone, like 2026-11-20T09:30:00Z")
    return value


StatedTime = Annotated[AwareDatetime, BeforeValidator(_time_as_text)]


class ApplicationIn(BaseModel):
    job_id: int = Field(ge=1, le=MAX_JOB_ID)
    application_packet_id: UUID | None = None  # the packet the person used, if one was made for this job
    occurred_at: StatedTime | None = None  # when they applied; now when left out


class StatusIn(BaseModel):
    status: ApplicationStatus
    occurred_at: StatedTime  # when it happened, as the person says


@router.get("/v1/applications", response_model=Envelope[list[ApplicationView]])
def applications(principal: Signed) -> dict[str, Any]:
    """The tracker: every application with its whole status history, the most recently changed first."""
    with db.transaction(principal.user_id) as conn:
        data = tracker.list_applications(conn, principal.user_id)
    return envelope(data)


@router.post("/v1/applications", status_code=201, response_model=Envelope[ApplicationView])
def record_application(body: ApplicationIn, write: Idempotent) -> Response:
    """Record that the person applied. One application per job: a second is 409 with state `application_exists`."""

    def effect(conn: Connection) -> ApplicationView:
        return tracker.create_application(
            conn, write.user_id, body.job_id, body.application_packet_id, body.occurred_at
        )

    return write.run(effect, status=201)


@router.post("/v1/applications/{application_id}/status", status_code=201, response_model=Envelope[ApplicationView])
def record_status(application_id: UUID, body: StatusIn, write: Idempotent) -> Response:
    """Record a new status (interview, offer, rejected, no response, or applied again) and when it happened. The same
    status as the current one is 409 `same_status`; a time before the current status or in the future is 422."""

    def effect(conn: Connection) -> ApplicationView:
        return tracker.record_status(conn, write.user_id, application_id, body.status, body.occurred_at)

    return write.run(effect, status=201)
