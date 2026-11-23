"""HTTP routes of the tailor module (API.md 2.9): request, read, decide and export a tailored resume.

    POST /v1/tailored-resumes                  ask for one for a job (202; a queue job builds it)
    GET  /v1/tailored-resumes                  the person's tailored resumes, newest first
    GET  /v1/tailored-resumes/{id}             one: status, the passed edits, and how many were discarded by reason
    POST /v1/tailored-resumes/{id}/decisions   approve or reject passed edits, each once
    GET  /v1/tailored-resumes/{id}/file        the resume with approved edits only, as ?format=pdf or docx

The person is always the session's. Someone else's tailored resume, edit or job is a 404. A discarded edit has no id
anywhere in a response, so it cannot be named, decided or exported (TLR-02). Handlers are plain `def`.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import service
from .export import UnsupportedCharacters, render_docx, render_pdf, resume_blocks
from .service import DecisionIn, TailoredView

router = APIRouter()

MAX_JOB_ID = 2**63 - 1  # job ids are bigints: a larger number cannot be one
MAX_DECISIONS = 100  # assumption: more than the edits of one tailored resume (the pipeline stops at 30)
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


class TailoredIn(BaseModel):
    job_id: int = Field(ge=1, le=MAX_JOB_ID)


class DecisionsIn(BaseModel):
    decisions: list[DecisionIn] = Field(min_length=1, max_length=MAX_DECISIONS)


@router.post("/v1/tailored-resumes", status_code=202, response_model=Envelope[TailoredView])
def request_tailored(body: TailoredIn, write: Idempotent) -> Response:
    """Ask for a tailored resume. It is built in the background: poll the read until its status is ready or failed."""

    def effect(conn: Connection) -> TailoredView:
        return service.request(conn, write.user_id, body.job_id)

    return write.run(effect, status=202)


@router.get("/v1/tailored-resumes", response_model=Envelope[list[TailoredView]])
def list_tailored(principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        data = service.list_for_user(conn, principal.user_id)
    return envelope(data)


@router.get("/v1/tailored-resumes/{tailored_id}", response_model=Envelope[TailoredView])
def read_tailored(tailored_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        data = service.get(conn, principal.user_id, tailored_id)
    return envelope(data)


@router.post("/v1/tailored-resumes/{tailored_id}/decisions", response_model=Envelope[TailoredView])
def decide(tailored_id: UUID, body: DecisionsIn, write: Idempotent) -> Response:
    """Approve or reject passed edits. An edit already decided is 409 `already_decided`; an id that is not a passed edit
    of this tailored resume is 404."""

    def effect(conn: Connection) -> TailoredView:
        return service.decide(conn, write.user_id, tailored_id, body.decisions)

    return write.run(effect)


@router.get("/v1/tailored-resumes/{tailored_id}/file")
def download(
    tailored_id: UUID, principal: Signed, file_format: Annotated[Literal["pdf", "docx"], Query(alias="format")]
) -> Response:
    """The resume with approved edits only. Rendered on every request and stored nowhere."""
    with db.transaction(principal.user_id) as conn:
        view = service.get(conn, principal.user_id, tailored_id)
        if view.status != "ready":
            raise ApiError(409, "conflict", "This tailored resume is not ready.", {"state": "not_ready"})
        doc = service.export_doc(conn, principal.user_id, tailored_id)
    name = f"tailored-resume-job-{view.job_id}.{file_format}"
    if file_format == "docx":
        return _file(render_docx(doc), DOCX_TYPE, name)
    try:
        return _file(render_pdf(resume_blocks(doc)), "application/pdf", name)
    except UnsupportedCharacters:
        raise ApiError(
            422,
            "validation_failed",
            "This resume has characters the PDF cannot draw. Download the DOCX instead.",
            {"fields": [{"field": "format", "issue": "pdf_unsupported_characters"}]},
        ) from None


def _file(content: bytes, media_type: str, name: str) -> Response:
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}"'})
