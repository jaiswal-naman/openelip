"""What this installation sends to whom (API.md 2.20, ARCHITECTURE 8.5), and what a person can take from it or have
removed from it: an export of their data, its status and download, and the deletion of their account (TRU-01, TRU-02).

The subprocessor list is built from the running configuration on every request, never hard-coded: it changes when
config/models.toml or the environment changes. Public. Everything else here needs a session, and a request is only ever
the session's own: another person's request is a 404.

The models are the contract: the web types are generated from them.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from app.platform import config, db, log, sessions
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.llm import registry
from app.platform.sessions import Principal, current_user

from . import auditlog, deletion, export

router = APIRouter(prefix="/v1/privacy")

OUTSIDE_COPIES = (
    "Copies made outside this system, for example an email that has already been delivered to someone else, "
    "cannot be reached by deletion."
)
# What a task of each data class sends (AI_SYSTEM 2.6). A class missing here fails the test that covers them all.
RECEIVES = {
    "public": "Public job text.",
    "de-identified": "Candidate facts with name, email, phone and links removed.",
    "identified": "Resume lines and chat messages, which can identify a person.",
    "synthetic": "Invented test messages only.",
}


class Subprocessor(BaseModel):
    name: str
    kind: str  # "model_provider" or "email"
    purpose: str
    receives: str  # in plain words, what is sent to it
    tasks: list[str]  # the model tasks it serves; empty for an email host


class Subprocessors(BaseModel):
    items: list[Subprocessor]
    outside_copies_statement: str  # copies made outside the system cannot be reached by deletion


def _model_providers(settings: config.Settings) -> list[Subprocessor]:
    """Only providers a task would be sent to right now: with no key, nothing leaves, so nothing is listed."""
    by_provider: dict[str, list[registry.Route]] = {}
    for route in registry.active_routes(settings):
        by_provider.setdefault(route.provider, []).append(route)
    items = []
    for routes in by_provider.values():
        classes = dict.fromkeys(r.data_class for r in routes)
        items.append(
            Subprocessor(
                name=routes[0].label,
                kind="model_provider",
                purpose=" ".join(r.description for r in routes),
                receives=" ".join([*(RECEIVES[c] for c in classes), routes[0].retention_note]),
                tasks=[r.task for r in routes],
            )
        )
    return sorted(items, key=lambda item: item.name)


def _email_host(settings: config.Settings) -> Subprocessor:
    host = urlsplit(settings.smtp_url).hostname or "Not stated by the operator"  # never the credentials in the URL
    return Subprocessor(
        name=host,
        kind="email",
        purpose="Delivers the sign-in codes this installation emails.",
        receives="Email addresses and the sign-in codes sent to them.",
        tasks=[],
    )


@router.get("/subprocessors", response_model=Envelope[Subprocessors])
def subprocessors() -> dict[str, Any]:
    settings = config.get()
    items = [*_model_providers(settings), _email_host(settings)]
    return envelope(Subprocessors(items=items, outside_copies_statement=OUTSIDE_COPIES))


# ---- export and deletion ---------------------------------------------------------------------------------------

API_STATUS = {"open": "queued", "in_progress": "running", "completed": "succeeded", "failed": "failed"}  # API.md 1.9
POLL_SECONDS = 5  # Retry-After on a request that is not finished (API.md 1.9)

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


class PrivacyDownload(BaseModel):
    method: Literal["GET"]
    path: str
    available_until: datetime


class PrivacyRequest(BaseModel):
    """An export or a deletion, with the API's shared status words (API.md 1.9)."""

    id: UUID
    kind: Literal["export", "deletion"]
    status: Literal["queued", "running", "succeeded", "failed"]
    requested_at: datetime
    completed_at: datetime | None
    download: PrivacyDownload | None  # an export that is ready and has not expired
    format: str | None  # an export: what the archive holds


class PrivacyRevoked(BaseModel):
    sessions: int  # agent tokens join this when they exist


class PrivacyDeletion(PrivacyRequest):
    revoked: PrivacyRevoked


class PrivacyDeletionBody(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")  # the emailed code, as at sign-in


# The caller's own request. Row-level security allows no other, and the query names the person as well.
REQUEST_SQL = """SELECT id, kind, status, created_at, completed_at, export_expires_at,
                        export_archive IS NOT NULL AND export_expires_at > now() AS downloadable
                 FROM data_requests WHERE id = :id AND subject_user_id = :u AND user_id = :u"""


def _read_request(conn: Connection, user_id: UUID, request_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(text(REQUEST_SQL), {"id": request_id, "u": user_id}).first()
    if row is None:
        return None
    download = None
    if row.downloadable:
        path = f"/v1/privacy/requests/{row.id}/download"
        download = {"method": "GET", "path": path, "available_until": row.export_expires_at}
    return {
        "id": row.id,
        "kind": row.kind,
        "status": API_STATUS[row.status],
        "requested_at": row.created_at,
        "completed_at": row.completed_at,
        "download": download,
        "format": export.FORMAT if row.kind == "export" else None,
    }


@router.post("/exports", status_code=202, response_model=Envelope[PrivacyRequest])
def create_export(write: Idempotent) -> Response:
    """Ask for an archive of everything held about the caller. Asking again while one is being made returns that one."""

    def effect(conn: Connection) -> dict[str, Any]:
        request = _read_request(conn, write.user_id, export.request_export(conn, write.user_id))
        return PrivacyRequest.model_validate(request).model_dump(mode="json")

    response = write.run(effect, status=202)
    # A replay of an earlier answer has no header of its own, so the id is read from the body for both.
    response.headers["Location"] = f"/v1/privacy/requests/{json.loads(bytes(response.body))['data']['id']}"
    return response


@router.get("/requests/{request_id}", response_model=Envelope[PrivacyRequest])
def get_request(request_id: UUID, principal: Signed, response: Response) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        request = _read_request(conn, principal.user_id, request_id)
    if request is None:
        raise ApiError(404, "not_found", "No such request.")
    if request["status"] in ("queued", "running"):
        response.headers["Retry-After"] = str(POLL_SECONDS)
    return envelope(request)


@router.get(
    "/requests/{request_id}/download",
    response_class=Response,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
def download_export(request_id: UUID, principal: Signed) -> Response:
    """The archive, opened from its seal for this one response. 409 while it is not ready, 410 once it has expired."""
    with db.transaction(principal.user_id) as conn:
        row = conn.execute(
            text(
                """SELECT kind, status, created_at, export_archive, export_expires_at > now() AS is_current
                   FROM data_requests WHERE id = :id AND subject_user_id = :u AND user_id = :u"""
            ),
            {"id": request_id, "u": principal.user_id},
        ).first()
    if row is None or row.kind != "export":
        raise ApiError(404, "not_found", "No such export.")
    if row.status != "completed":
        raise ApiError(409, "conflict", "The export is not ready.", {"state": API_STATUS[row.status]})
    if row.export_archive is None or not row.is_current:
        # 410, a kind of not found: the catalogue (API.md 1.11) has no code for an archive that was removed on purpose
        raise ApiError(410, "not_found", "This export has expired. Ask for a new one.", {"state": "expired"})
    return Response(
        sessions.open_sealed_bytes(bytes(row.export_archive)),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="data-export-{row.created_at:%Y-%m-%d}.zip"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/deletion", status_code=202, response_model=Envelope[PrivacyDeletion])
def delete_account(body: PrivacyDeletionBody, write: Idempotent) -> Response:
    """Close the caller's account at once and queue the erasure of everything about them. Needs a fresh emailed code.

    The Idempotency-Key is required (API.md 1.6) but not used to replay: the single-use code plays that part, and once
    this has worked the session is gone, so a retry cannot get here. Nothing runs inside write.run for the same reason:
    a wrong code must be counted, and an error raised inside the transaction would roll the count back.
    """
    with db.transaction(write.user_id) as conn:
        outcome = deletion.request_deletion(conn, write.user_id, body.code)
        answer = None
        if not isinstance(outcome, int):
            request = _read_request(conn, write.user_id, outcome.request_id)
            revoked = {"sessions": outcome.revoked_sessions}
            answer = PrivacyDeletion.model_validate({**(request or {}), "revoked": revoked}).model_dump(mode="json")
    if isinstance(outcome, int):  # the wrong try was counted and committed above; refuse it now
        log.event("privacy.deletion_requested", outcome="code_invalid")
        raise ApiError(401, "code_invalid", "That code does not match or has expired.", {"attempts_remaining": outcome})
    log.event("privacy.deletion_requested", outcome="accepted", data_request_id=outcome.request_id)
    response = JSONResponse(envelope(answer), status_code=202)
    response.delete_cookie(sessions.COOKIE_NAME, **sessions.cookie_options())
    return response


class AuditOrganization(BaseModel):
    id: str
    name: str | None  # None where the caller may not read the organization's name


class AuditLink(BaseModel):
    kind: str
    href: str


class AuditLogEntry(BaseModel):
    """One automated decision or event about the caller. `summary` is built by code from codes and counts."""

    id: str
    occurred_at: datetime
    source: str  # account, matching, guardian (and twin, once the Twin exists)
    kind: str
    actor_type: str
    summary: str
    outcome: str | None
    reason_codes: list[str]
    agent_token_id: UUID | None
    campaign_id: UUID | None
    organization: AuditOrganization | None
    link: AuditLink | None


class AuditLogPage(BaseModel):
    entries: list[AuditLogEntry]
    next_cursor: str | None


@router.get("/audit-log", response_model=Envelope[AuditLogPage])
def get_audit_log(
    principal: Signed,
    agent_token_id: Annotated[UUID | None, Query()] = None,
    campaign_id: Annotated[UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=auditlog.MAX_LIMIT)] = auditlog.DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Every automated decision and every agent action about the caller, newest first, from the sources each module
    registered. Everything runs on the caller's own API connection: another person's entries are not visible to it."""
    try:
        before = auditlog.decode_cursor(cursor) if cursor else None
    except auditlog.BadCursor:
        raise ApiError(400, "invalid_request", "The cursor is not valid.") from None
    with db.transaction(principal.user_id) as conn:
        entries, next_cursor = auditlog.read(
            conn, principal.user_id, auditlog.AuditFilter(agent_token_id, campaign_id, before, limit)
        )
    page = AuditLogPage(
        entries=[AuditLogEntry.model_validate(e, from_attributes=True) for e in entries], next_cursor=next_cursor
    )
    return envelope(page.model_dump(mode="json"), next_cursor=next_cursor, limit=limit)
