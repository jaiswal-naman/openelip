"""Tailored resumes as a product feature (TLR-01 to TLR-03): request one, generate it in the worker, read it, decide
each edit and render the file (plan 05-05).

What a person sees is only ever a passed edit. The worker stores every edit the pipeline returns, passed and discarded
alike, so the discard count is real; the read returns the passed ones and a count by reason, and a discarded edit's text
leaves the database nowhere but in the person's own data archive (DATA_MODEL 7.3). Invariant 3 is checked three times:
by the pipeline, by the database trigger at insert (a broken verifier fails the whole tailored resume), and again by the
export, which draws only approved edits onto a base built from the person's confirmed facts.
"""

import logging
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.ingest import api as ingest
from app.match import api as match
from app.platform import config, log
from app.platform.errors import ApiError
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, registry
from app.platform.queue import NonRetryable, enqueue
from app.profile import api as profile
from app.trust import api as trust

from . import pipeline
from .models import EDIT_DISCARD_REASONS
from .resume import Fact, ResumeDoc, apply_edits, build_base, derive_op
from .verifier import Requirement

JOB_KIND = "tailor.generate"
QUEUE = "interactive"
TAILORED_PER_DAY = 10  # assumption: tailored resumes a person may ask for in a rolling 24 hours (API.md 1.8)
WINDOW = timedelta(hours=24)
TASKS = (pipeline.PROPOSE_TASK, pipeline.ENTAIL_TASK)

Status = Literal["generating", "ready", "failed"]

ACCOUNT = text("SELECT display_name, email, record_version FROM users WHERE id = :u")
_RECENT = """SELECT count(*) AS n, min(created_at) AS oldest FROM {table}
              WHERE user_id = :u AND created_at > now() - make_interval(secs => :window)"""
RECENT = {table: text(_RECENT.format(table=table)) for table in ("tailored_resumes", "application_packets")}
INSERT = text(
    """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version)
       VALUES (:u, :j, :v, :doc, :version) RETURNING id"""
)
ONE = text(
    """SELECT id, job_id, job_content_version, record_version, status::text AS status, prompt_version, discarded_count,
              created_at FROM tailored_resumes WHERE user_id = :u AND id = :id"""
)
LISTED = text(
    """SELECT id, job_id, job_content_version, record_version, status::text AS status, prompt_version, discarded_count,
              created_at FROM tailored_resumes WHERE user_id = :u ORDER BY created_at DESC, id LIMIT :limit"""
)
LATEST_READY = text(
    """SELECT id FROM tailored_resumes WHERE user_id = :u AND job_id = :j AND status = 'ready'
        ORDER BY created_at DESC, id LIMIT 1"""
)
DEAD = text("SELECT 1 FROM queue_jobs WHERE unique_key = :key AND status = 'dead'")
LAST_ATTEMPT = text("SELECT attempts >= max_attempts FROM queue_jobs WHERE unique_key = :key")
PASSED_EDITS = text(
    """SELECT id, ordinal, section, target_fact_id, before_text, after_text, supporting_fact_ids,
              decision::text AS decision, decided_at
         FROM resume_edits WHERE user_id = :u AND tailored_resume_id = :r AND verifier_status = 'passed'
        ORDER BY ordinal"""
)
DISCARDED = text(
    """SELECT discard_reason::text AS reason, count(*) AS n FROM resume_edits
        WHERE user_id = :u AND tailored_resume_id = :r AND verifier_status = 'discarded' GROUP BY 1"""
)
INSERT_EDIT = text(
    """INSERT INTO resume_edits (tailored_resume_id, user_id, ordinal, section, target_fact_id, before_text, after_text,
                                supporting_fact_ids, verifier_status, discard_reason)
       VALUES (:r, :u, :ordinal, :section, CAST(:target AS uuid), :before, :after, CAST(:support AS uuid[]),
               CAST(:status AS edit_verifier_status), CAST(:reason AS edit_discard_reason))"""
)
LOCK = text("""SELECT id, user_id, job_id, status::text AS status FROM tailored_resumes WHERE id = :id FOR UPDATE""")
READY = text(
    """UPDATE tailored_resumes SET status = 'ready', prompt_version = :version, discarded_count = :discarded
        WHERE id = :id"""
)
FAILED = text("UPDATE tailored_resumes SET status = 'failed' WHERE id = :id AND status = 'generating'")
EDIT_OF_RESUME = text(
    """SELECT id, decision::text AS decision FROM resume_edits
        WHERE user_id = :u AND tailored_resume_id = :r AND verifier_status = 'passed' AND id = ANY(CAST(:ids AS uuid[]))
        FOR UPDATE"""
)
DECIDE = text(
    """UPDATE resume_edits SET decision = CAST(:decision AS edit_decision), decided_at = now()
        WHERE user_id = :u AND id = :id"""
)
SUPPORT_OF = text(
    """SELECT supporting_fact_ids FROM resume_edits WHERE user_id = :u AND id = ANY(CAST(:ids AS uuid[]))"""
)
APPROVED_EDITS = text(
    """SELECT target_fact_id, before_text, after_text, supporting_fact_ids, section FROM resume_edits
        WHERE user_id = :u AND tailored_resume_id = :r AND verifier_status = 'passed' AND decision = 'approved'
        ORDER BY ordinal"""
)


class EditView(BaseModel):
    """One passed edit, as the person sees it. A discarded edit has no view."""

    id: UUID
    ordinal: int
    op: str  # derived: rephrase, reorder, remove or add_from_fact (resume_edits has no op column)
    section: str
    before_text: str
    after_text: str
    supporting_fact_ids: list[UUID]
    decision: str  # pending, approved or rejected
    decided_at: datetime | None


class TailoredView(BaseModel):
    id: UUID
    job_id: int
    job_title: str
    company_name: str
    status: Status
    discarded_count: int  # always shown: how many edits were thrown away before the person saw any (TLR-02)
    discarded_by_reason: dict[str, int]  # counts only; a discarded edit's text is never returned
    prompt_version: str | None
    record_version: int
    created_at: datetime
    edits: list[EditView]


class DecisionIn(BaseModel):
    edit_id: UUID
    decision: Literal["approved", "rejected"]


def not_found() -> ApiError:
    return ApiError(404, "not_found", "No such resource.")


def to_fact(row: dict[str, Any]) -> Fact:
    """A usable fact from profile.api as tailoring reads it."""
    return Fact(
        id=str(row["id"]),
        kind=row["kind"],
        statement=row["statement"],
        attributes=row["attributes"] or {},
        parent_fact_id=row["parent_fact_id"],
        source=row["source"],
    )


def base_document_id(rows: list[dict[str, Any]]) -> UUID | None:
    """The parsed resume the record was last read from: the document of the newest confirmed fact that came from one. A
    person whose facts all came from chat or by hand has no resume to tailor."""
    for row in reversed(rows):  # usable_facts lists them oldest first
        provenance = row.get("provenance")
        if provenance and provenance.get("document_id"):
            return UUID(provenance["document_id"])
    return None


def require_ai(conn: Connection, user_id: UUID) -> None:
    """503 ai_disabled when either task has no route, then 403 consent_required: the order the resume upload uses (with
    no provider there is nothing to consent to). Packets use this too."""
    for task in TASKS:
        registry.route(task, config.get())
    trust.require_consent(conn, user_id, "ai_processing")


def rate_limited(conn: Connection, user_id: UUID, table: str, per_day: int) -> None:
    """429 with the wait, counted from rows so it survives a restart (decision 21). `table` is tailored_resumes or
    application_packets."""
    recent = conn.execute(RECENT[table], {"u": user_id, "window": WINDOW.total_seconds()}).one()
    if recent.n >= per_day:
        wait = int((recent.oldest + WINDOW - datetime.now(timezone.utc)).total_seconds())
        raise ApiError(
            429, "rate_limited", "Too many requests. Try again later.", {"retry_after_seconds": max(1, wait)}
        )


def open_job(conn: Connection, job_id: int) -> dict[str, Any]:
    """The job, or 404; a closed one is 410 job_closed (there is nothing left to tailor for)."""
    job = ingest.get_job(conn, job_id)
    if job is None:
        raise not_found()
    if job["status"] == "closed":
        raise ApiError(410, "job_closed", "This job is closed.")
    return job


# ---- request ----------------------------------------


def request(conn: Connection, user_id: UUID, job_id: int) -> TailoredView:
    """Start a tailored resume: the row in `generating` and its queue job, in the caller's transaction."""
    job = open_job(conn, job_id)
    require_ai(conn, user_id)
    document = base_document_id(profile.usable_facts(conn, user_id))
    if document is None:
        raise ApiError(409, "conflict", "Upload a resume first.", {"state": "no_resume"})
    rate_limited(conn, user_id, "tailored_resumes", TAILORED_PER_DAY)
    version = conn.execute(ACCOUNT, {"u": user_id}).one().record_version
    created = conn.execute(
        INSERT,
        {"u": user_id, "j": job_id, "v": job["content_version"], "doc": document, "version": version},
    ).scalar_one()
    enqueue(
        conn,
        JOB_KIND,
        {"tailored_resume_id": str(created)},
        unique_key=f"{JOB_KIND}:{created}",
        queue=QUEUE,
        user_id=user_id,
    )
    return get(conn, user_id, created)


# ---- generate (the worker) ----------------------------------------


def _fail(conn: Connection, tailored_id: UUID, code: str) -> None:
    """Record the failure and commit it: the queue rolls back a handler that raises."""
    conn.rollback()
    conn.execute(FAILED, {"id": tailored_id})
    conn.commit()
    log.event(
        "tailor.generate", level=logging.WARNING, tailored_resume_id=tailored_id, status="failed", error_code=code
    )


def _last_attempt(conn: Connection, tailored_id: UUID) -> bool:
    """True when the queue will not run this job again. A tailored resume with no queue row (a direct call) is not."""
    return bool(conn.execute(LAST_ATTEMPT, {"key": f"{JOB_KIND}:{tailored_id}"}).scalar())


def generate(conn: Connection, payload: dict[str, Any]) -> None:
    """The handler of tailor.generate: propose, verify and store every edit, then mark the resume ready.

    AIDisabled, missing consent and an answer that stays invalid fail it (NonRetryable); a provider that did not
    answer is left for the queue to retry, and fails it on the last attempt. A passed edit the database refuses
    (invariant 3) fails it too, so no edit of a broken run can be read."""
    tailored_id = UUID(payload["tailored_resume_id"])
    row = conn.execute(LOCK, {"id": tailored_id}).one_or_none()
    if row is None or row.status != "generating":
        return  # gone, or a run that overlapped this one finished first
    user_id, job_id = row.user_id, row.job_id
    try:
        trust.require_consent(conn, user_id, "ai_processing")
        facts = [to_fact(f) for f in profile.usable_facts(conn, user_id)]
        job = ingest.get_job(conn, job_id) or {}
        requirements = [Requirement(r["id"], r["text"], r["importance"]) for r in job.get("requirements", [])]
        decision = _shown_decision(conn, user_id, job_id)
        conn.commit()  # no transaction stays open while the models answer
        proposal = pipeline.propose_and_verify(
            facts,
            requirements,
            decision,
            principal=user_id,
            allow_rephrase=config.get().tailor_rephrase,
        )
        _store(conn, tailored_id, user_id, proposal)
    except trust.ConsentRequired:
        _fail(conn, tailored_id, "consent_required")
        raise NonRetryable("consent_required") from None
    except AIDisabled:
        _fail(conn, tailored_id, "ai_disabled")
        raise NonRetryable("ai_disabled") from None
    except SchemaInvalid:
        _fail(conn, tailored_id, "schema_invalid")
        raise NonRetryable("schema_invalid") from None
    except ProviderUnavailable:
        conn.rollback()
        if _last_attempt(conn, tailored_id):
            _fail(conn, tailored_id, "provider_unavailable")
        raise
    except DBAPIError as error:  # invariant 3 at insert, or any other refusal: nothing of this run stays
        code = "unsupported_edit" if "unsupported_edit" in str(error.orig) else "store_failed"
        _fail(conn, tailored_id, code)
        raise NonRetryable(code) from None


def _shown_decision(conn: Connection, user_id: UUID, job_id: int) -> dict[int, str]:
    """What the matching step found for each requirement, when the person was shown a decision for this job."""

    decision = match.decision_for_job(conn, user_id, job_id)
    if decision is None or decision.outcome != "shown":
        return {}
    return {e.requirement_id: e.verdict for e in decision.evidence}


def _store(conn: Connection, tailored_id: UUID, user_id: UUID, proposal: pipeline.Proposal) -> None:
    """Every edit, passed or discarded, and the ready mark, in one transaction."""
    for edit in proposal.edits:
        conn.execute(
            INSERT_EDIT,
            {
                "r": tailored_id,
                "u": user_id,
                "ordinal": edit.ordinal,
                "section": edit.section,
                "target": edit.target_fact_id,
                "before": edit.before_text,
                "after": edit.after_text,
                "support": list(edit.supporting_fact_ids),
                "status": edit.verifier_status,
                "reason": edit.discard_reason,
            },
        )
    discarded = sum(edit.verifier_status == "discarded" for edit in proposal.edits)
    conn.execute(READY, {"id": tailored_id, "version": proposal.prompt_version, "discarded": discarded})
    log.event(
        "tailor.generate",
        tailored_resume_id=tailored_id,
        status="ready",
        passed=len(proposal.edits) - discarded,
        discarded=discarded,
    )


# ---- read ----------------------------------------


def _view(conn: Connection, user_id: UUID, row: Any) -> TailoredView:
    job = ingest.get_job(conn, row.job_id) or {}
    status: Status = row.status
    if status == "generating" and conn.execute(DEAD, {"key": f"{JOB_KIND}:{row.id}"}).first() is not None:
        status = "failed"  # a dead queue job is reported as failed (INDEX queue kinds)
    edits: list[EditView] = []
    by_reason = dict.fromkeys(EDIT_DISCARD_REASONS, 0)
    if status == "ready":
        for e in conn.execute(PASSED_EDITS, {"u": user_id, "r": row.id}):
            edits.append(
                EditView(
                    id=e.id,
                    ordinal=e.ordinal,
                    op=derive_op(_text(e.target_fact_id), e.before_text, e.after_text),
                    section=e.section,
                    before_text=e.before_text,
                    after_text=e.after_text,
                    supporting_fact_ids=e.supporting_fact_ids,
                    decision=e.decision,
                    decided_at=e.decided_at,
                )
            )
        for found in conn.execute(DISCARDED, {"u": user_id, "r": row.id}):
            by_reason[found.reason] = found.n
    return TailoredView(
        id=row.id,
        job_id=row.job_id,
        job_title=job.get("title", ""),
        company_name=job.get("company_name", ""),
        status=status,
        discarded_count=row.discarded_count if status == "ready" else 0,
        discarded_by_reason={reason: n for reason, n in by_reason.items() if n},
        prompt_version=row.prompt_version,
        record_version=row.record_version,
        created_at=row.created_at,
        edits=edits,
    )


def _text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def get(conn: Connection, user_id: UUID, tailored_id: UUID) -> TailoredView:
    row = conn.execute(ONE, {"u": user_id, "id": tailored_id}).one_or_none()
    if row is None:
        raise not_found()
    return _view(conn, user_id, row)


def list_for_user(conn: Connection, user_id: UUID, limit: int = 50) -> list[TailoredView]:
    """The person's tailored resumes, newest first."""
    return [_view(conn, user_id, row) for row in conn.execute(LISTED, {"u": user_id, "limit": limit})]


def latest_ready_for_job(conn: Connection, user_id: UUID, job_id: int) -> UUID | None:
    """The id of the person's newest ready tailored resume for the job."""
    found: UUID | None = conn.execute(LATEST_READY, {"u": user_id, "j": job_id}).scalar_one_or_none()
    return found


# ---- decide ----------------------------------------


def decide(conn: Connection, user_id: UUID, tailored_id: UUID, decisions: list[DecisionIn]) -> TailoredView:
    """Approve or reject passed edits, each once. An id that is not a passed edit of this tailored resume (a discarded
    edit's too) is 404; an edit already decided is 409 `already_decided`. All or none."""
    if conn.execute(ONE, {"u": user_id, "id": tailored_id}).one_or_none() is None:
        raise not_found()
    ids = [d.edit_id for d in decisions]
    current = {
        row.id: row.decision for row in conn.execute(EDIT_OF_RESUME, {"u": user_id, "r": tailored_id, "ids": ids})
    }
    if any(edit_id not in current for edit_id in ids):
        raise not_found()
    if any(current[edit_id] != "pending" for edit_id in ids) or len(set(ids)) != len(ids):
        raise ApiError(409, "conflict", "An edit can be decided once.", {"state": "already_decided"})
    approving = [d.edit_id for d in decisions if d.decision == "approved"]
    if approving:
        usable = usable_fact_ids(conn, user_id)
        stored = conn.execute(SUPPORT_OF, {"u": user_id, "ids": approving})
        if any(not {str(i) for i in row.supporting_fact_ids} <= usable for row in stored):
            raise ApiError(
                409, "conflict", "A fact this edit rests on is no longer in your record.", {"reason": "fact_not_usable"}
            )
    for d in decisions:
        conn.execute(DECIDE, {"u": user_id, "id": d.edit_id, "decision": d.decision})
    return get(conn, user_id, tailored_id)


# ---- export ----------------------------------------


def usable_fact_ids(conn: Connection, user_id: UUID) -> set[str]:
    """The ids of the person's usable (confirmed, current) facts, as text."""
    return {str(f["id"]) for f in profile.usable_facts(conn, user_id)}


def edit_is_usable(supporting_fact_ids: Iterable[Any], usable: set[str]) -> bool:
    """True while every fact an edit rests on is still usable (invariant 3: the record is the only source)."""
    return {str(i) for i in supporting_fact_ids} <= usable


def export_doc(conn: Connection, user_id: UUID, tailored_resume_id: UUID) -> ResumeDoc:
    """The resume as it stands with approved edits only: the base built from the person's confirmed facts now, then each
    approved edit. A rejected, undecided or discarded edit never reaches it. An approved edit is also dropped here when
    any one of its supporting facts is no longer usable (retired, rejected or superseded since): apply_edits only drops
    an edit whose target bullet left the base, which is not enough for an added fact or a second supporting fact. Every
    export (the file, the packet PDF, the archive DOCX) goes through this function, so an export never prints a sentence
    the record no longer holds."""
    if conn.execute(ONE, {"u": user_id, "id": tailored_resume_id}).one_or_none() is None:
        raise not_found()
    base = base_doc(conn, user_id)
    usable = usable_fact_ids(conn, user_id)
    edits = [
        e
        for e in conn.execute(APPROVED_EDITS, {"u": user_id, "r": tailored_resume_id})
        if edit_is_usable(e.supporting_fact_ids, usable)
    ]
    return apply_edits(base, [_Stored(e) for e in edits])


class _Stored:
    """A resume_edits row in the shape apply_edits reads: text ids."""

    def __init__(self, row: Any) -> None:
        self.target_fact_id = _text(row.target_fact_id)
        self.before_text = row.before_text
        self.after_text = row.after_text
        self.supporting_fact_ids = tuple(str(i) for i in row.supporting_fact_ids)
        self.section = row.section


def base_doc(conn: Connection, user_id: UUID) -> ResumeDoc:
    """The base resume built from the person's confirmed facts now. The header is the name and email of their account
    (users.display_name, users.email: what GET /v1/me reads); a person with no name gets an empty one, never a guess."""
    account = conn.execute(ACCOUNT, {"u": user_id}).one()
    facts = [to_fact(f) for f in profile.usable_facts(conn, user_id)]
    return build_base(facts, name=account.display_name or "", email=account.email)
