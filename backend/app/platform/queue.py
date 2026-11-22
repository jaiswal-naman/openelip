"""The Postgres queue (ARCHITECTURE 5.1 and 5.7; requirement PLT-06).

A job is a row of queue_jobs. enqueue() inserts it on the caller's connection, so the job exists exactly when the change
that caused it does, and its unique key stays taken for the row's whole life (until prune() deletes the row). run_once()
claims one due row with FOR UPDATE SKIP LOCKED, runs its handler in a transaction of its own and records the outcome.

Delivery is at least once: a worker that dies mid-job leaves a lease that reap() hands back, so a handler must be safe
to run twice. Everything stored or logged about a failure is the exception's class name, never its message, because a
message can carry the text of a resume, an email address or a job description (PLT-07).
"""

import json
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from random import random
from typing import Any

from sqlalchemy import Connection, text

from . import log

Handler = Callable[[Connection, dict[str, Any]], None]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]

# After the 1st failed attempt wait 30 s, after the 2nd 2 min, then 10 min, 30 min, 2 h; plus up to 10% jitter.
BACKOFF_SECONDS = (30, 120, 600, 1800, 7200)
JITTER = 0.10
KEEP_SUCCEEDED_DAYS = 7
KEEP_DEAD_DAYS = 30

_handlers: dict[str, Handler] = {}


class NonRetryable(Exception):
    """A handler raises this (or a subclass) for a failure a retry cannot fix, such as invalid input: dead at once."""


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    queue: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Decorator that registers fn(conn, payload) as the handler for jobs of this kind."""

    def register(fn: Handler) -> Handler:
        if kind in _handlers:
            raise ValueError(f"two handlers registered for {kind!r}")
        _handlers[kind] = fn
        return fn

    return register


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    unique_key: str,
    queue: str = "default",
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Add a job in the caller's transaction and return its id, or None when the unique key already exists.

    The id is an int because queue_jobs.id is a bigint (DATA_MODEL 3.13), not a UUID as the plans first wrote.
    Nothing is logged here: the caller's transaction may still roll back. The first log line for a job is its claim.
    """
    row = conn.execute(
        text(
            """INSERT INTO queue_jobs (queue, kind, payload, unique_key, run_at, max_attempts)
               VALUES (:queue, :kind, CAST(:payload AS jsonb), :unique_key, COALESCE(:run_at, now()), :max_attempts)
               ON CONFLICT (unique_key) DO NOTHING
               RETURNING id"""
        ),
        {
            "queue": queue,
            "kind": kind,
            "payload": json.dumps(payload),
            "unique_key": unique_key,
            "run_at": run_at,
            "max_attempts": max_attempts,
        },
    ).first()
    return None if row is None else int(row[0])


def claim(conn: Connection, worker_id: str, queues: Sequence[str] = ()) -> Job | None:
    """Take the most urgent due job (lowest priority number, then oldest run_at), or None when nothing is due.

    SKIP LOCKED means two workers never receive the same row and never wait for each other. attempts counts the attempt
    that starts now, so a job that kills its worker still runs out of attempts. An empty queues list means every queue.
    """
    row = conn.execute(
        text(
            """UPDATE queue_jobs
                  SET status = 'running', locked_by = :worker, locked_at = now(), attempts = attempts + 1
                WHERE id = (SELECT id FROM queue_jobs
                             WHERE status = 'queued' AND run_at <= now() AND attempts < max_attempts
                               AND (cardinality(CAST(:queues AS text[])) = 0 OR queue = ANY(CAST(:queues AS text[])))
                             ORDER BY priority, run_at
                             FOR UPDATE SKIP LOCKED
                             LIMIT 1)
            RETURNING id, kind, queue, payload, attempts, max_attempts"""
        ),
        {"worker": worker_id, "queues": list(queues)},
    ).first()
    if row is None:
        return None
    return Job(int(row.id), str(row.kind), str(row.queue), dict(row.payload), int(row.attempts), int(row.max_attempts))


def run_once(conn_factory: ConnectionFactory, worker_id: str, queues: Sequence[str] = ()) -> int:
    """Claim and run at most one due job; returns 1 if it ran one (whatever the outcome) and 0 if none was due.

    One job per call keeps the worker loop returning to the scheduler and the email relay between jobs; the loop calls
    again straight away while there is work. conn_factory yields a connection whose block commits when it ends normally
    and rolls back when it raises (db.transaction does). The claim commits before the handler starts, so the lease is
    visible to the other workers. The handler's own work and the "succeeded" mark share one transaction.

    ponytail: one-second poll in the worker loop. LISTEN/NOTIFY if claim latency ever matters (revisit at p95 > 1 s).
    """
    with conn_factory() as conn:
        job = claim(conn, worker_id, queues)
    if job is None:
        return 0
    started = time.monotonic()
    _log(job, "running")
    fn = _handlers.get(job.kind)
    code: str | None = None
    retry = True
    if fn is None:
        code, retry = "unknown_kind", False
    else:
        try:
            with conn_factory() as conn:
                fn(conn, job.payload)
                conn.execute(
                    text(
                        """UPDATE queue_jobs SET status = 'succeeded', finished_at = now(), locked_by = NULL,
                                  locked_at = NULL WHERE id = :id"""
                    ),
                    {"id": job.id},
                )
        except NonRetryable as error:
            code, retry = type(error).__name__, False
        except Exception as error:  # any handler failure is the job's, never the worker's
            code = type(error).__name__
    if code is None:
        _log(job, "succeeded", started=started)
        return 1
    with conn_factory() as conn:
        status = _record_failure(conn, job, code, retry)
    _log(job, status, started=started, error_code=code)
    return 1


def _record_failure(conn: Connection, job: Job, code: str, retry: bool) -> str:
    """Back to queued with a delay, or dead when retrying is pointless or the attempts are used up."""
    if retry and job.attempts < job.max_attempts:
        delay = BACKOFF_SECONDS[min(job.attempts, len(BACKOFF_SECONDS)) - 1] * (1 + JITTER * random())
        conn.execute(
            text(
                """UPDATE queue_jobs SET status = 'queued', last_error_code = :code, locked_by = NULL, locked_at = NULL,
                          run_at = now() + make_interval(secs => :delay) WHERE id = :id"""
            ),
            {"id": job.id, "code": code, "delay": delay},
        )
        return "queued"
    conn.execute(
        text(
            """UPDATE queue_jobs SET status = 'dead', last_error_code = :code, finished_at = now(), locked_by = NULL,
                      locked_at = NULL WHERE id = :id"""
        ),
        {"id": job.id, "code": code},
    )
    return "dead"


def reap(conn: Connection) -> int:
    """Hand back running jobs whose lease (locked_at + lease_seconds) has passed; returns how many.

    A job that has used its last attempt goes to dead instead, so a job that kills its worker cannot loop forever.
    """
    rows = conn.execute(
        text(
            """UPDATE queue_jobs
                  SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END::queue_status,
                      finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
                      last_error_code = 'lease_expired', locked_by = NULL, locked_at = NULL
                WHERE status = 'running' AND locked_at + lease_seconds * interval '1 second' < now()
            RETURNING id, kind, queue, status, attempts"""
        )
    ).all()
    for row in rows:
        log.event(
            "queue.job",
            job_id=row.id,
            kind=row.kind,
            queue=row.queue,
            status=row.status,
            attempts=row.attempts,
            error_code="lease_expired",
        )
    return len(rows)


def prune(conn: Connection) -> int:
    """Delete succeeded jobs older than 7 days and dead ones older than 30; returns how many.

    Once a row is gone its unique key can be used again.
    """
    deleted = conn.execute(
        text(
            """DELETE FROM queue_jobs
                WHERE (status = 'succeeded' AND finished_at < now() - make_interval(days => :succeeded))
                   OR (status = 'dead' AND finished_at < now() - make_interval(days => :dead))"""
        ),
        {"succeeded": KEEP_SUCCEEDED_DAYS, "dead": KEEP_DEAD_DAYS},
    ).rowcount
    log.event("queue.pruned", deleted=deleted)
    return deleted


def _log(job: Job, status: str, error_code: str | None = None, *, started: float | None = None) -> None:
    """One queue.job line per state change: ids, enumerations, counts and a duration, nothing else."""
    duration_ms = None if started is None else round((time.monotonic() - started) * 1000)
    log.event(
        "queue.job",
        job_id=job.id,
        kind=job.kind,
        queue=job.queue,
        status=status,
        attempts=job.attempts,
        duration_ms=duration_ms,
        error_code=error_code,
    )
