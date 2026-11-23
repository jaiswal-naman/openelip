"""Queue handlers for the job index: ingest.fetch (one source), ingest.dedupe, ingest.discover (ARCHITECTURE 12.4),
ingest.fetch_sweep, which queues the refresh every six hours (IDX-07), ingest.embed_sweep, which gives every new or
changed job its vector every five minutes (IDX-04), and ingest.extract with its 15-minute ingest.extract_sweep, which
read each job's requirements once per content version when a model can be called (IDX-08, see extraction.py).

Each source is its own job, so a slow or failing board delays only itself and a repeat of the same slot is a no-op
(unique key ingest.fetch:{source_id}:{slot}). A fetch that fails or comes back empty is not a failed job: it is a
fetch_runs row with its status, and the jobs already stored stay open (the foundation rule, unchanged).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import Connection, text

from app.platform import log
from app.platform.queue import enqueue, handler
from app.platform.scheduler import every

from . import discover, embedding, extraction
from .adapters import URLS, fetch_postings
from .fetch import mark_duplicates, store

# How often every active source is fetched. ARCHITECTURE 5.3 (an assumption to tune after measuring).
FETCH_EVERY_HOURS = 6
FETCH_PERIOD_SECONDS = FETCH_EVERY_HOURS * 3600

# How often the jobs that have no vector yet are embedded. An assumption to tune after measuring.
EMBED_EVERY_MINUTES = 5
every("ingest.embed_sweep", EMBED_EVERY_MINUTES * 60)(embedding.embed_sweep)

# How often jobs whose requirements are missing or out of date are queued for extraction, which needs a model route
# (the sweep queues nothing without one). An assumption to tune after measuring.
EXTRACT_EVERY_MINUTES = 15
every("ingest.extract_sweep", EXTRACT_EVERY_MINUTES * 60)(extraction.extract_sweep)
handler("ingest.extract")(extraction.extract)

# The first fetch of a slot queues the slot's one dedupe job, this long after, so most of the batch is in by then.
# ponytail: a fixed wait. Enqueue it from the last fetch of the slot once the slot's job count is tracked.
DEDUPE_DELAY = timedelta(minutes=10)


def queue_fetches(conn: Connection, now: datetime) -> int:
    """Queue one ingest.fetch for every active source in the six-hour slot that holds `now`; returns how many were new.

    The slot is the number of whole six-hour periods since 1970, so the key ingest.fetch:{source_id}:{slot} is the same
    for every worker and every repeat in that period, and the queue's unique key discards the repeat. With no active
    source there is nothing to fetch, so the slot's one discovery job is queued instead (it registers the boards that
    exist, and the next slot fetches them).
    """
    slot = int(now.timestamp()) // FETCH_PERIOD_SECONDS
    source_ids = conn.execute(text("SELECT id FROM job_sources WHERE is_active ORDER BY id")).scalars().all()
    if not source_ids:
        enqueue(conn, "ingest.discover", {}, unique_key=f"ingest.discover:{slot}", queue="ingest")
        log.event("ingest.fetch_sweep", slot=slot, sources=0, queued=0)
        return 0
    queued = sum(
        enqueue(
            conn,
            "ingest.fetch",
            {"source_id": source_id, "slot": slot},
            unique_key=f"ingest.fetch:{source_id}:{slot}",
            queue="ingest",
        )
        is not None
        for source_id in source_ids
    )
    log.event("ingest.fetch_sweep", slot=slot, sources=len(source_ids), queued=queued)
    return queued


@every("ingest.fetch_sweep", FETCH_PERIOD_SECONDS)
def fetch_sweep(conn: Connection, payload: dict[str, Any]) -> None:
    queue_fetches(conn, datetime.now(timezone.utc))


async def fetch_feed(ats: str, token: str) -> tuple[str, int | None, list[dict[str, Any]] | None, str | None]:
    """The existing adapter call. It refuses any ats or token outside the three fixed hosts before it touches the
    network, and it never follows a redirect."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        return await fetch_postings(client, ats, token)


@handler("ingest.fetch")
def fetch_source(conn: Connection, payload: dict[str, Any]) -> None:
    source_id = payload["source_id"]
    source = conn.execute(
        text("SELECT ats, token, company_name FROM job_sources WHERE id = :id AND is_active"), {"id": source_id}
    ).one_or_none()
    if source is None:
        return  # deactivated after the job was queued: nothing to fetch
    status, http_status, postings, error = asyncio.run(fetch_feed(source.ats, source.token))
    counts: dict[str, Any] = {"seen": 0, "new": 0, "changed": 0, "closed": 0}
    if status == "ok":
        counts = store(conn, source_id, source.company_name, postings or [])
        status = counts.pop("status")
    conn.execute(
        text(
            """INSERT INTO fetch_runs (source_id, finished_at, status, http_status, jobs_seen, jobs_new, jobs_changed,
                                       jobs_closed, error)
               VALUES (:source_id, now(), CAST(:status AS fetch_status), :http_status, :seen, :new, :changed, :closed,
                       :error)"""
        ),
        {"source_id": source_id, "status": status, "http_status": http_status, "error": error, **counts},
    )
    # A failed or suspicious fetch is one more failure in a row, and an ok one ends the run of them.
    conn.execute(
        text(
            """UPDATE job_sources SET last_fetched_at = now(), last_status = CAST(:status AS fetch_status),
                      consecutive_failures = CASE WHEN :is_ok THEN 0 ELSE consecutive_failures + 1 END
                WHERE id = :id"""
        ),
        {"status": status, "is_ok": status == "ok", "id": source_id},
    )
    log.event("ingest.fetch", source_id=source_id, status=status, http_status=http_status, error_code=error, **counts)
    slot = payload["slot"]
    enqueue(
        conn,
        "ingest.dedupe",
        {"slot": slot},
        unique_key=f"ingest.dedupe:{slot}",
        queue="ingest",
        run_at=datetime.now(timezone.utc) + DEDUPE_DELAY,
    )


@handler("ingest.dedupe")
def dedupe(conn: Connection, payload: dict[str, Any]) -> None:
    log.event("ingest.dedupe", folded=mark_duplicates(conn))


@handler("ingest.discover")
def discover_sources(conn: Connection, payload: dict[str, Any]) -> None:
    tokens = discover.candidate_tokens()
    found = asyncio.run(discover.find_live_boards(tokens))
    discover.register(conn, found)
    log.event(
        "ingest.discover",
        candidates=len(tokens),
        live_boards=len(found),
        postings=sum(board[3] for board in found),
        **{ats: sum(1 for board in found if board[0] == ats) for ats in URLS},
    )
