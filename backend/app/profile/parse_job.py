"""The profile.parse job: a stored resume becomes unconfirmed facts, each with the line it came from (ARCHITECTURE 4.2;
REC-01, REC-06, TRU-05).

Five steps. Steps 2 to 4 can end the parse with a code the candidate reads on the document, and the job is then done:
a retry could not change the answer.
  1. Claim. The document becomes "parsing" and that is committed, so a poll says "running". A document that is gone,
     parsed or failed ends the job here, which is why a second run of one document adds nothing. So does a document
     whose owner's account is no longer active (deletion requested, or suspended): nothing is read or sent.
  2. Read the file. Hidden text is removed and counted. A corrupt, encrypted or scanned file fails (corrupt, encrypted,
     scanned).
  3. Consent and route. ai_processing is required as it stands now, so a consent withdrawn after the upload stops the
     parse before the gateway sees anything (consent_required). So does a route that is no longer available: no
     provider, or a real model that no passed gold-set run proves (ai_gate; ai_disabled).
  4. Extract the facts through the gateway (a provider with no answer to give, such as the fake with no recording for
     the task: ai_disabled).
  5. One transaction writes the text, the hidden-text count, status parsed and every fact, so a crash leaves nothing or
     everything.

A provider that did not answer, and any unexpected error, are left to the queue to retry. On the last attempt the
document is failed as well, so it never stays "running" for good, and the error goes on to dead-letter the job.
Nothing here logs text: the document id, counts and codes only (PLT-07).
"""

import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Row, insert, text
from sqlalchemy.dialects.postgresql import Range

from app.platform import log
from app.platform.llm import AIDisabled, ProviderUnavailable, complete
from app.platform.sessions import open_sealed_bytes
from app.trust.api import ConsentRequired, require_consent

from . import ai_gate, models
from .documents import job_key
from .extract.schemas import UnverifiedItem
from .extract.validate import MAX_STATEMENT
from .extract.workflow import ExtractionResult, Gateway, extract_facts
from .resume.reader import ReadResult, ResumeUnreadable, read_resume

# Only an active account's document is claimed. A person who has asked for deletion (status deleting) must not have
# their resume sent to a provider by a parse that was queued before they asked; the erase job removes the document.
CLAIM = text(
    """UPDATE documents SET status = 'parsing'
        WHERE id = :id AND status IN ('uploaded', 'parsing')
          AND EXISTS (SELECT 1 FROM users u WHERE u.id = documents.user_id AND u.status = 'active')
       RETURNING user_id, mime_type, content"""
)
LOCK = text("SELECT status FROM documents WHERE id = :id FOR UPDATE")
PARSED = text(
    """UPDATE documents SET status = 'parsed', extracted_text = :body, hidden_text_chars = :hidden, parsed_at = now(),
              parse_error_code = :code WHERE id = :id"""
)
FAILED = text(
    """UPDATE documents SET status = 'failed', parse_error_code = :code
       WHERE id = :id AND status IN ('uploaded', 'parsing')"""
)
LAST_ATTEMPT = text("SELECT attempts >= max_attempts FROM queue_jobs WHERE unique_key = :key")


def parse(conn: Connection, payload: dict[str, Any], *, gateway: Gateway = complete) -> None:
    """The handler of profile.parse. gateway is app.platform.llm.complete, or a stand-in called the same way."""
    document_id = UUID(payload["document_id"])
    claimed = conn.execute(CLAIM, {"id": document_id}).first()
    conn.commit()  # a poll now says "running"
    if claimed is None:
        return
    try:
        _run(conn, document_id, claimed, gateway)
    except Exception as error:  # the provider did not answer, or something unexpected: the queue retries it
        conn.rollback()
        if not _is_last_attempt(conn, document_id):
            raise
        _fail(conn, document_id, "provider_unavailable" if isinstance(error, ProviderUnavailable) else "parse_failed")
        conn.commit()  # the queue rolls this transaction back when the error goes on, so keep the failure first
        raise


def _run(conn: Connection, document_id: UUID, claimed: Row[Any], gateway: Gateway) -> None:
    try:
        result = read_resume(open_sealed_bytes(bytes(claimed.content)), claimed.mime_type)
    except ResumeUnreadable as unreadable:
        _fail(conn, document_id, unreadable.code)
        return
    if result.scanned:
        _fail(conn, document_id, "scanned")
        return
    try:
        require_consent(conn, claimed.user_id, "ai_processing")
        ai_gate.require_available(conn)
    except ConsentRequired:
        _fail(conn, document_id, "consent_required")
        return
    except AIDisabled:
        _fail(conn, document_id, "ai_disabled")
        return
    conn.commit()  # no transaction stays open while the model answers
    try:
        extracted = extract_facts(gateway, result, attribution=claimed.user_id)
    except AIDisabled:
        _fail(conn, document_id, "ai_disabled")
        return
    _store(conn, document_id, claimed.user_id, result, extracted)


def _fail(conn: Connection, document_id: UUID, code: str) -> None:
    conn.execute(FAILED, {"id": document_id, "code": code})
    log.event("profile.parse", level=logging.WARNING, document_id=document_id, status="failed", error_code=code)


def _is_last_attempt(conn: Connection, document_id: UUID) -> bool:
    """True when the queue will not run this job again. A document with no queue row (a direct call) is not."""
    return bool(conn.execute(LAST_ATTEMPT, {"key": job_key(document_id)}).scalar())


def _store(conn: Connection, document_id: UUID, user_id: UUID, result: ReadResult, extracted: ExtractionResult) -> None:
    if conn.execute(LOCK, {"id": document_id}).scalar_one_or_none() not in ("uploaded", "parsing"):
        return  # a run that overlapped this one finished first, or the document is gone
    # The text goes in first: the database checks each fact's quote against it as the fact is inserted.
    conn.execute(
        PARSED,
        {
            "id": document_id,
            "body": result.text,
            "hidden": result.hidden.chars_removed,
            "code": "partial_extraction" if extracted.failed_calls else None,
        },
    )
    rows = _fact_rows(document_id, user_id, extracted)
    if rows:
        # One statement, because a parent may come after its child in the list and a foreign key is checked per
        # statement, not per row.
        conn.execute(insert(models.profile_facts).values(rows))
    log.event(
        "profile.parse",
        document_id=document_id,
        status="parsed",
        kept=len(extracted.kept),
        unverified=len(rows) - len(extracted.kept),
        hidden_chars=result.hidden.chars_removed,
    )


def _fact_rows(document_id: UUID, user_id: UUID, extracted: ExtractionResult) -> list[dict[str, Any]]:
    """Every fact the file supports, then what the model pointed at and the code could not confirm, as unconfirmed rows.

    An unconfirmed row is one the candidate has not looked at, so nothing here is used anywhere (REC-02). An item whose
    lines do not exist has nothing to quote and is dropped; so is one too long to be a statement, and a second item at
    the same lines. The rest become facts of kind other, with the reason on them for the candidate to see."""
    common = {"user_id": user_id, "source": "resume", "document_id": document_id}
    ids = [uuid4() for _ in extracted.kept]
    rows = [
        {
            **common,
            "id": ids[position],
            "kind": fact.kind,
            "statement": fact.statement,
            "attributes": fact.attributes,
            "parent_fact_id": None if fact.parent_index is None else ids[fact.parent_index],
            "source_span": Range(*fact.source_span),
            "source_quote": fact.source_quote,
        }
        for position, fact in enumerate(extracted.kept)
    ]
    quoted: dict[tuple[int, int], UnverifiedItem] = {}
    for item in extracted.unverified:
        if item.source_span is not None and item.source_quote and len(item.source_quote) <= MAX_STATEMENT:
            quoted.setdefault(item.source_span, item)
    for span, item in quoted.items():
        rows.append(
            {
                **common,
                "id": uuid4(),
                "kind": "other",
                "statement": item.source_quote,
                "attributes": {"unverified": True, "reason": item.reason},
                "parent_fact_id": None,
                "source_span": Range(*span),
                "source_quote": item.source_quote,
            }
        )
    return rows
