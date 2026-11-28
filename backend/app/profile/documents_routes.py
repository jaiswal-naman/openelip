"""Upload a resume and read how its parse is going: POST /v1/me/documents and GET /v1/me/documents/{id} (API.md 2.3).

The upload is the one multipart write, so it cannot use the JSON-only idempotent() dependency, and it has to be careful
with a body that may be large and is not trusted (T-3-16, T-3-17). It declares its checks as dependencies, in the order
FastAPI runs them, and none of them reads the body: the session, the Idempotency-Key header, a model provider for the
resume tasks (503 ai_disabled), then the consent (403 consent_required). Only then is the body read, with its size
counted as it arrives, and a request that crosses the cap is refused at that point, not after it has all been received.

A missing provider is reported before a missing consent: with no model there is nothing to agree to, and
telling the person to agree would not help. The routes here are mounted by routes.py under /v1/me.
"""

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict
from sqlalchemy import Connection, Row, text
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import KEY_PATTERN, Write
from app.platform.sessions import Principal, current_user, open_sealed_bytes
from app.trust.api import require_consent

from . import ai_gate, documents
from .errors import InvalidField, RecordNotFound
from .models import DOCUMENT_API_STATUS
from .resume.reader import read_resume

router = APIRouter()

MAX_REQUEST_BYTES = (
    documents.MAX_BYTES + 64 * 1024
)  # the file, and the few hundred bytes of multipart framing around it
POLL_SECONDS = 2  # the Retry-After hint while a parse has not finished (API.md 1.9)
# Text from the candidate's own file: clients show it as text and never as markup (API.md 1.10).
UNTRUSTED: JsonDict = {"x-untrusted-text": True}
# The body is read by hand, so FastAPI cannot describe it: this puts it in the OpenAPI document for the web's types.
UPLOAD_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary", "description": "A PDF or DOCX, at most 10 MB."}
                    },
                }
            }
        },
    }
}

Signed = Annotated[Principal, Depends(current_user)]
ApiStatus = Literal["queued", "running", "succeeded", "failed"]

# One row: the document, how many facts it produced (and how many of those still wait for review), and the two heavy
# columns only when the answer needs them: the text for ?include=text, the file when some hidden text was removed.
STATUS = text(
    """SELECT d.id, d.original_filename, d.mime_type, d.byte_size, d.status, d.parse_error_code, d.hidden_text_chars,
              d.created_at, d.parsed_at,
              CASE WHEN :with_text AND d.status = 'parsed' THEN d.extracted_text END AS body,
              CASE WHEN d.status = 'parsed' AND d.hidden_text_chars > 0 THEN d.content END AS sealed,
              count(f.id) FILTER (WHERE f.attributes ->> 'unverified' IS DISTINCT FROM 'true') AS kept,
              count(f.id) FILTER (WHERE f.attributes ->> 'unverified' = 'true') AS unverified,
              count(f.id) FILTER (WHERE f.confirmed_at IS NULL AND f.rejected_at IS NULL
                                    AND f.superseded_at IS NULL) AS to_review
       FROM documents d LEFT JOIN profile_facts f ON f.document_id = d.id
       WHERE d.id = :id AND d.user_id = :user GROUP BY d.id"""
)


# ---- what the API returns ------------------------------------------------------------------------


class DocumentQueued(BaseModel):
    id: UUID
    status: Literal["queued"]


class DocumentFacts(BaseModel):
    """What the parse produced from this file: facts the code could confirm, items it could not (kept as unconfirmed
    facts of kind other, with the reason), and how many of all of them the candidate has not looked at yet."""

    kept: int
    unverified: int
    to_review: int


class DocumentHiddenText(BaseModel):
    """What was removed from the file as hidden or off-page text (REC-06): why, and a few samples to show the candidate.
    Recomputed from the stored file, because the table keeps only the count."""

    reasons: list[str]
    samples: list[str] = Field(json_schema_extra=UNTRUSTED)


class DocumentLine(BaseModel):
    n: int  # 1-based
    start: int  # text[start:end] is the line, the same offsets the facts' spans use
    end: int
    text: str = Field(json_schema_extra=UNTRUSTED)


class DocumentView(BaseModel):
    """parse_error_code says why a document failed (or, on a succeeded one, that part of it could not be read:
    partial_extraction); message is the same in words. hidden_text_flags is null until the document is parsed."""

    id: UUID
    status: ApiStatus
    original_filename: str | None = Field(json_schema_extra=UNTRUSTED)
    mime_type: str
    byte_size: int
    parse_error_code: str | None
    message: str | None
    facts: DocumentFacts
    hidden_text_chars: int
    hidden_text_flags: DocumentHiddenText | None
    created_at: datetime
    parsed_at: datetime | None
    text: str | None = Field(default=None, json_schema_extra=UNTRUSTED)  # only with ?include=text
    lines: list[DocumentLine] | None = None  # only with ?include=text


# ---- the upload ----------------------------------------------------------------------------------


def upload_key(idempotency_key: Annotated[str | None, Header()] = None) -> str:
    """The Idempotency-Key of an upload (API.md 1.6). platform.idempotency.idempotent cannot serve here: it reads the
    whole body and insists on JSON."""
    if idempotency_key is None:
        raise ApiError(400, "idempotency_key_missing", "Send an Idempotency-Key header with every write.")
    key = idempotency_key.strip().strip('"')
    if not KEY_PATTERN.fullmatch(key):
        raise ApiError(400, "invalid_request", "The Idempotency-Key must be 8 to 128 visible characters.")
    return key


def require_ai(principal: Signed) -> None:
    """503 ai_disabled unless a provider serves every resume task (ai_gate; a real model nobody has measured is served),
    then 403 consent_required unless the person has agreed to AI processing as it is worded now. A dependency, so it
    runs before the body is read."""
    with db.transaction(principal.user_id) as conn:
        ai_gate.require_available(conn)
        require_consent(conn, principal.user_id, "ai_processing")


@dataclass(frozen=True)
class Upload:
    filename: str | None
    data: bytes


async def capped(stream: AsyncIterator[bytes], limit: int) -> AsyncGenerator[bytes, None]:
    """The chunks of a request body, ending in 413 as soon as more than limit bytes have arrived."""
    seen = 0
    async for chunk in stream:
        seen += len(chunk)
        if seen > limit:
            raise documents.PayloadTooLarge()
        yield chunk


async def read_upload(request: Request) -> Upload:
    """The one file of a multipart request, in the field named file. The cap is applied to the stream before the parser
    sees it, and the file is kept in memory: the parser would write one over 1 MB to a temporary file on disk, which
    would leave a resume there unencrypted for the length of the request, and it is read into memory next anyway."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise ApiError(415, "unsupported_media_type", "Send the file as multipart/form-data, in a field named file.")
    parser = MultiPartParser(request.headers, capped(request.stream(), MAX_REQUEST_BYTES), max_files=1, max_fields=0)
    parser.spool_max_size = MAX_REQUEST_BYTES
    try:
        form = await parser.parse()
    except MultiPartException:  # its message describes the framing, not the content, but there is nothing to act on
        raise ApiError(400, "invalid_request", "The upload is not valid multipart form data.") from None
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise InvalidField("file", "required")
        return Upload(file.filename, await file.read())
    finally:
        await form.close()


@router.post("/documents", status_code=202, response_model=Envelope[DocumentQueued], openapi_extra=UPLOAD_BODY)
def upload_document(
    request: Request,
    principal: Signed,
    key: Annotated[str, Depends(upload_key)],
    _ai: Annotated[None, Depends(require_ai)],
    upload: Annotated[Upload, Depends(read_upload)],
) -> Response:
    """Keep a resume and queue its parse: 202 with the document's id, and a Location to poll. The same key with the
    same file answers with the first answer and stores nothing more. The parameters are checked in the order written."""
    fingerprint = hashlib.sha256(upload.data).digest()
    write = Write(principal.user_id, key, request.method, request.url.path, fingerprint)

    def effect(conn: Connection) -> dict[str, Any]:
        document_id = documents.store_document(conn, write.user_id, upload.filename, upload.data)
        return DocumentQueued(id=document_id, status="queued").model_dump(mode="json")

    response = write.run(effect, status=202)
    document_id = json.loads(bytes(response.body))["data"]["id"]
    response.headers["Location"] = f"/v1/me/documents/{document_id}"
    return response


# ---- the status ----------------------------------------------------------------------------------


def _hidden_text(row: Row[Any]) -> dict[str, list[str]] | None:
    """Why and what was removed. ponytail: the file is opened and read again on every request that needs this, which is
    only a parsed document with removed text; store the samples or cache them if that ever shows in a profile."""
    if row.status != "parsed":
        return None
    if row.sealed is None:  # no hidden text was removed
        return {"reasons": [], "samples": []}
    hidden = read_resume(open_sealed_bytes(bytes(row.sealed)), row.mime_type).hidden
    return {"reasons": hidden.reasons, "samples": hidden.samples}


def _lines(body: str) -> list[dict[str, Any]]:
    """The stored text as numbered lines. The reader joined its lines with a newline and no line holds one, so the split
    gives back the lines the extraction was shown, with offsets into the text the facts' spans point into."""
    lines, start = [], 0
    for number, line in enumerate(body.split("\n"), 1):
        lines.append({"n": number, "start": start, "end": start + len(line), "text": line})
        start += len(line) + 1
    return lines


@router.get("/documents/{document_id}", response_model=Envelope[DocumentView])
def get_document(
    document_id: UUID, principal: Signed, response: Response, include: Literal["text"] | None = None
) -> dict[str, Any]:
    """How a parse is going and what it made. Poll this until status is succeeded or failed (Retry-After says how long
    to wait). ?include=text adds the cleaned text the facts' spans point into, and its lines. Another person's document
    is a 404, like one that does not exist."""
    with db.transaction(principal.user_id) as conn:
        row = conn.execute(
            STATUS, {"id": document_id, "user": principal.user_id, "with_text": include == "text"}
        ).first()
    if row is None:
        raise RecordNotFound()
    api_status = DOCUMENT_API_STATUS[row.status]
    if api_status in ("queued", "running"):
        response.headers["Retry-After"] = str(POLL_SECONDS)
    return envelope(
        {
            "id": row.id,
            "status": api_status,
            "original_filename": row.original_filename,
            "mime_type": row.mime_type,
            "byte_size": row.byte_size,
            "parse_error_code": row.parse_error_code,
            "message": documents.PARSE_MESSAGES.get(row.parse_error_code or ""),
            "facts": {"kept": row.kept, "unverified": row.unverified, "to_review": row.to_review},
            "hidden_text_chars": row.hidden_text_chars,
            "hidden_text_flags": _hidden_text(row),
            "created_at": row.created_at,
            "parsed_at": row.parsed_at,
            "text": row.body,
            "lines": None if row.body is None else _lines(row.body),
        }
    )
