"""Requirements for every open job, once per content version (IDX-08, AI_SYSTEM 4.3).

ingest.extract_sweep runs every 15 minutes and queues one ingest.extract for each open, canonical, listed job whose
requirements are not those of its current text, but only when a real model can be called: with no key requirements are
not extracted at all (ARCHITECTURE 10.4), and the sweep says so. It also queues nothing while the route is unproven: a
route may serve a task only when a passed gold-set run for that task and model exists (AI_SYSTEM 2.2), so the sweep
asks eval_runs for a passed requirement_extraction run on the test split, by the model method, whose config names the
route's model and the current prompt version. ingest.extract asks the gateway (task job.requirements.extract) and, in
the one transaction the queue gives a handler, stores the rows, marks the version done and clears the job's vector, so
the embed sweep re-embeds it from its requirement texts (AI_SYSTEM 3.2).

The text of a version never changes (jobs_version_guard), so a row stored for version v quotes version v for good. A
posting that changes while the model works is left for the next sweep.
"""

import logging
import time
from typing import Any

from sqlalchemy import Connection, text

from app.platform import config, log
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, registry
from app.platform.queue import NonRetryable, enqueue

from .requirements import task

# Jobs queued by one sweep: it bounds what a burst of new postings can spend (T-02-31). An assumption, to tune after
# measuring. ponytail: a fixed number; make it a setting if the cadence ever needs tuning.
SWEEP_LIMIT = 200

# Oldest change first. A job whose extraction was ever queued keeps its queue key until the queue prunes the row, so one
# whose answer was unusable is not asked again, and does not hold up the jobs behind it either.
CANDIDATES = text(
    """SELECT j.id, j.content_version
         FROM jobs j
        WHERE j.status = 'open' AND j.canonical_job_id IS NULL AND j.is_listed
          AND j.requirements_version IS DISTINCT FROM j.content_version
          AND NOT EXISTS (SELECT 1 FROM queue_jobs q
                           WHERE q.unique_key = 'ingest.extract:' || j.id || ':' || j.content_version)
        ORDER BY j.materially_changed_at, j.id
        LIMIT :limit"""
)
# Whether a passed gold-set run proves this route (AI_SYSTEM 2.2): the model method on the test split, naming the model
# and the prompt version in use. The replay CI records names no model, so it never proves a route; a live run must name
# its model in its config (evals/requirement_extraction/README.md).
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE suite = 'requirement_extraction' AND status = 'passed'
            AND config->>'method' = 'model' AND config->>'split' = 'test'
            AND config->>'model' = :model AND config->>'prompt_version' = :prompt_version)"""
)
# No row lock while the model works: a fetch must not wait for it.
READ_JOB = text(
    """SELECT description, content_version, requirements_version, status = 'open' AS is_open,
              canonical_job_id IS NULL AS is_canonical, is_listed
         FROM jobs WHERE id = :id"""
)
# The version guard: this is the one statement that decides whether the answer is still wanted. It also clears the
# vector (and the model that made it, which a CHECK ties to it) so the embed sweep makes a new one from the rows.
MARK_DONE = text(
    """UPDATE jobs SET requirements_version = content_version, embedding = NULL, embedding_model = NULL
        WHERE id = :id AND content_version = :version AND requirements_version IS DISTINCT FROM content_version"""
)
# skill_ids keep their default: no skills taxonomy is chosen yet. llm_call_id stays NULL: complete() returns only the
# answer, so the ledger row of this call is not known here, and an id is never guessed.
# ponytail: NULL until the gateway returns the ledger id with the answer.
INSERT_ROW = text(
    """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span, min_years,
                                    prompt_version)
       VALUES (:job_id, :version, :ordinal, CAST(:importance AS requirement_importance),
               CAST(:kind AS requirement_kind), :body, int4range(:start, :end), :min_years, :prompt_version)"""
)


class ExtractionRejected(NonRetryable):
    """The model's answer cannot be used: invalid twice, or refused. Asking again fails the same way and costs the
    same, so the queue buries the job. str() is the kind, a word and never the answer."""


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _fail(job_id: int, kind: str, started: float) -> None:
    log.event("ingest.extract", level=logging.WARNING, job_id=job_id, error_code=kind, duration_ms=_ms(started))


def extract(conn: Connection, payload: dict[str, Any]) -> None:
    """The ingest.extract job: payload {job_id, content_version}. Safe to run twice and safe against a fetch that
    changes the posting meanwhile (both end without a write). A failure of the model's side is raised for the queue:
    no route, a provider error and a timeout are retried with backoff, an unusable answer is not."""
    job_id, version = payload["job_id"], payload["content_version"]
    started = time.monotonic()
    job = conn.execute(READ_JOB, {"id": job_id}).one_or_none()
    if (
        job is None
        or not (job.is_open and job.is_canonical and job.is_listed)
        or job.content_version != version
        or job.requirements_version == version
    ):
        log.event("ingest.extract", job_id=job_id, outcome="skipped", duration_ms=_ms(started))
        return
    try:
        result = task.run(job.description)
    except AIDisabled:
        _fail(job_id, "unavailable", started)
        raise
    except ProviderUnavailable as unavailable:
        _fail(job_id, "timeout" if unavailable.timed_out else "provider_error", started)
        raise
    except SchemaInvalid as invalid:
        kind = "refused" if invalid.refused else "invalid_output"
        _fail(job_id, kind, started)
        raise ExtractionRejected(kind) from None
    if conn.execute(MARK_DONE, {"id": job_id, "version": version}).rowcount != 1:
        # The text changed while the model worked, or another worker stored this version first: this answer is dropped.
        log.event("ingest.extract", job_id=job_id, outcome="stale", duration_ms=_ms(started))
        return
    if result.rows:  # the span trigger checks every row again against the text of this version
        conn.execute(
            INSERT_ROW,
            [
                {
                    "job_id": job_id,
                    "version": version,
                    "ordinal": row.ordinal,
                    "importance": row.importance,
                    "kind": row.kind,
                    "body": row.text,
                    "start": row.start,
                    "end": row.end,
                    "min_years": row.min_years,
                    "prompt_version": task.PROMPT_VERSION,
                }
                for row in result.rows
            ],
        )
    log.event(
        "ingest.extract",
        job_id=job_id,
        outcome="extracted",
        rows=len(result.rows),
        rejected=sum(result.rejected.values()),
        duration_ms=_ms(started),
    )


def _unavailable(conn: Connection) -> str | None:
    """Why no model may be called for this task, or None when one may. The fake provider replays recordings and none
    exist for live postings, so it counts as no model here: nothing is queued that could only fail. A real route
    nobody has measured is "unproven": a key alone must not start spending on postings with a model of unknown quality.
    The check is made when jobs are queued; a job already queued runs on."""
    try:
        route = registry.route(task.TASK, config.get())
    except AIDisabled as disabled:
        return disabled.reason
    if route.adapter == "fake":
        return "replay_only"
    proven = conn.execute(ROUTE_IS_PROVEN, {"model": route.model, "prompt_version": task.PROMPT_VERSION}).scalar_one()
    return None if proven else "unproven"


def extract_sweep(conn: Connection, payload: dict[str, Any]) -> None:
    """The scheduled job (ingest.extract_sweep): queue extraction for the jobs that need it, a few at a time."""
    if (reason := _unavailable(conn)) is not None:
        log.event("ingest.extract_sweep", status="ai_disabled", error_code=reason, queued=0)
        return
    due = conn.execute(CANDIDATES, {"limit": SWEEP_LIMIT}).all()
    queued = sum(
        enqueue(
            conn,
            "ingest.extract",
            {"job_id": row.id, "content_version": row.content_version},
            unique_key=f"ingest.extract:{row.id}:{row.content_version}",
            queue="ingest",
        )
        is not None
        for row in due
    )
    log.event("ingest.extract_sweep", candidates=len(due), queued=queued)
