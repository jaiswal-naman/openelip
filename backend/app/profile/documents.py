"""Storing an uploaded resume: check the size, sniff the type, encrypt the bytes, and queue the parse in the same
transaction as the row (ARCHITECTURE 4.2; REC-01, REC-06; threat T-3-16).

The bytes are trusted for nothing. The type comes from the file's own first bytes (the client's content type and the
file's name are ignored), the size is capped, and the file is kept as a Fernet token with the SHA-256 of the original.
The filename is kept as display text only and is never used as a path.
"""

import hashlib
import io
import re
import zipfile
from uuid import UUID

from sqlalchemy import Connection, insert

from app.platform import queue
from app.platform.errors import ApiError
from app.platform.sessions import seal_bytes

from . import models
from .resume.reader import DOCX_MIME, PDF_MIME

MAX_BYTES = 10_485_760  # 10 MB, the limit of documents.byte_size (DATA_MODEL 3.4; an assumption)
MAX_FILENAME = 255
PARSE_JOB = "profile.parse"
PARSE_QUEUE = "interactive"
PARSE_ATTEMPTS = 4  # the first try and three retries, 30 s, 2 min and 10 min apart (queue.BACKOFF_SECONDS)

# What the candidate is told when a document ends as failed, or parsed with part of it missing. The code is the
# contract; the words are for people and may change.
PARSE_MESSAGES = {
    "corrupt": "We could not read this file. Check that it opens on your computer, then upload it again.",
    "encrypted": "This file is password protected. Remove the password and upload it again.",
    "scanned": "We could not read text from this file. If it is a scanned image, upload a text PDF or a DOCX.",
    "consent_required": "AI features are off because you have not agreed to AI processing, so this file was not read.",
    "ai_disabled": "This feature needs an AI model and none is configured, so this file was not read.",
    "provider_unavailable": "The AI model provider did not answer. Try uploading the file again shortly.",
    "parse_failed": "Something went wrong while reading this file. Try uploading it again.",
    "partial_extraction": "Part of your resume could not be read automatically. Check the facts and add what is gone.",
}


class PayloadTooLarge(ApiError):
    def __init__(self) -> None:
        super().__init__(
            413, "payload_too_large", f"The file is over {MAX_BYTES // 1_048_576} MB.", {"max_bytes": MAX_BYTES}
        )


class UnsupportedMedia(ApiError):
    def __init__(self) -> None:
        super().__init__(
            415, "unsupported_media_type", "Upload a PDF or a DOCX file.", {"accepted": [PDF_MIME, DOCX_MIME]}
        )


def job_key(document_id: UUID) -> str:
    """The queue's unique key for a document's parse: one job per document, whatever happens to the request."""
    return f"{PARSE_JOB}:{document_id}"


def sniff(data: bytes) -> str:
    """The type from the bytes: a PDF starts with %PDF-, and a DOCX is a ZIP that holds word/document.xml. Anything
    else, an empty file included, is UnsupportedMedia."""
    if data.startswith(b"%PDF-"):
        return PDF_MIME
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:  # reads the directory only, not the members
                if "word/document.xml" in archive.namelist():
                    return DOCX_MIME
        except zipfile.BadZipFile:
            pass
    raise UnsupportedMedia()


def clean_filename(name: str | None) -> str | None:
    """The name as display text: no folders, no control or invisible characters, at most MAX_FILENAME characters."""
    base = re.split(r"[\\/]", name or "")[-1]
    shown = "".join(char for char in base if char.isprintable())[:MAX_FILENAME].strip()
    return shown or None


def store_document(conn: Connection, user_id: UUID, filename: str | None, data: bytes) -> UUID:
    """Keep the file for this person and queue its parse, both in the caller's transaction: the row and the job exist
    together or not at all. Raises PayloadTooLarge or UnsupportedMedia before anything is written."""
    if len(data) > MAX_BYTES:
        raise PayloadTooLarge()
    mime = sniff(data)
    document_id: UUID = conn.execute(
        insert(models.documents)
        .values(
            user_id=user_id,
            content=seal_bytes(data),
            original_filename=clean_filename(filename),
            mime_type=mime,
            byte_size=len(data),
            sha256=hashlib.sha256(data).digest(),
        )
        .returning(models.documents.c.id)
    ).scalar_one()
    queue.enqueue(
        conn,
        PARSE_JOB,
        {"document_id": str(document_id)},
        unique_key=job_key(document_id),
        queue=PARSE_QUEUE,
        max_attempts=PARSE_ATTEMPTS,
    )
    return document_id
