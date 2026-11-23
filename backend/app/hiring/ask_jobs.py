"""The queue handlers that ask candidates: hiring.ask_candidates and the 15-minute hiring.sweep (HIR-04, HIR-06).

hiring.ask_candidates {role_id}: one role run and the asks it leads to. Its keys are hiring.ask:{role_id}:published
(made when a role is published) and hiring.ask:{role_id}:{slot} (made by the sweep, one a day), so a repeated enqueue
is one job.
hiring.sweep: every 15 minutes it (1) expires asks nobody answered in seven days, (2) schedules a role run for each
open role of an unpaused organization that has had none in the last day, and (3) asks the selections of a run finished
in the last day that were held back, which is how asking resumes within 15 minutes of an overdue answer being given.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.match import api as match
from app.platform import log, queue, scheduler

from . import asks

ROLE_RUN_INTERVAL_SECONDS = 24 * 60 * 60  # assumption (06-INDEX): a role is run again after a day while it is open
SCORE_LIMIT = 40  # assumption: candidates scored per role run, which bounds a run's spend
SWEEP_SECONDS = 900
ASK_KIND = "hiring.ask_candidates"
ASK_QUEUE = "llm"  # the run calls a model, like a candidate's nightly run


def ask_key(role_id: UUID, slot: int | str) -> str:
    return f"hiring.ask:{role_id}:{slot}"


@queue.handler(ASK_KIND)
def ask_candidates(conn: Connection, payload: dict[str, Any]) -> None:
    """Run an open role against its pool and ask the candidates it selects. A closed or unknown role, or an organization
    that owes an overdue answer, does nothing at all: no run, no model call, no ask."""
    role = asks.open_role(conn, UUID(str(payload["role_id"])))
    if role is None:
        log.event("hiring.ask_skipped", outcome="role_not_open")
        return
    if asks.is_paused(conn, role.organization_id):
        log.event("hiring.ask_skipped", role_id=role.id, outcome="answers_overdue")
        return
    candidates = asks.pool(conn, role)
    if not candidates:  # an empty run would replace the counts the employer sees (role_preview reads the latest run)
        log.event("hiring.ask_skipped", role_id=role.id, outcome="empty_pool")
        return
    result = match.run_role(role.id, role.job_id, candidates, score_limit=SCORE_LIMIT)
    outcome = asks.ask_from_run(conn, role, result.run_id)
    log.event(
        "hiring.ask_job",
        role_id=role.id,
        run_id=result.run_id,
        retrieved=len(candidates),
        introduced=len(result.selected),
        asked=outcome.asked,
        skipped=outcome.skipped,
        outcome="paused" if outcome.paused else "done",
    )


def _expire(conn: Connection) -> int:
    """Asks past their seven days become expired: a silence is a no (HIR-04). The guard allows the move only then."""
    return conn.execute(
        text("UPDATE introductions SET status = 'expired' WHERE status = 'asked' AND expires_at <= now()")
    ).rowcount


def _schedule_runs(conn: Connection, now: datetime) -> int:
    """Enqueue an ask job for each open role of an unpaused organization that has had no role run in the last day, has
    none running and no unfinished ask job."""
    slot = scheduler.slot_of(now, ROLE_RUN_INTERVAL_SECONDS)
    due = conn.execute(
        text(
            """SELECT r.id FROM roles r
                WHERE r.status = 'open' AND r.job_id IS NOT NULL
                  AND NOT organization_has_overdue_answers(r.organization_id)
                  AND NOT EXISTS (SELECT 1 FROM match_runs m
                                   WHERE m.role_id = r.id AND m.trigger = 'role'
                                     AND (m.status = 'running'
                                          OR m.started_at > now() - make_interval(secs => :seconds)))
                  AND NOT EXISTS (SELECT 1 FROM queue_jobs q
                                   WHERE q.kind = :kind AND q.status IN ('queued', 'running')
                                     AND q.payload->>'role_id' = CAST(r.id AS text))
                ORDER BY r.id"""
        ),
        {"seconds": ROLE_RUN_INTERVAL_SECONDS, "kind": ASK_KIND},
    ).scalars()
    queued = 0
    for role_id in due:
        job = queue.enqueue(
            conn, ASK_KIND, {"role_id": str(role_id)}, unique_key=ask_key(role_id, slot), queue=ASK_QUEUE
        )
        queued += job is not None
    return queued


def _resume(conn: Connection) -> int:
    """Ask the selections of a role run finished in the last day that no introduction answers yet: the ones an
    answer_clock refusal, a failed preview or a restart left behind. Only open roles of unpaused organizations."""
    runs = conn.execute(
        text(
            """SELECT DISTINCT ON (r.id) r.id AS role_id, m.id AS run_id FROM roles r
                 JOIN match_runs m ON m.role_id = r.id AND m.trigger = 'role' AND m.status = 'completed'
                WHERE r.status = 'open' AND r.job_id IS NOT NULL
                  AND m.finished_at > now() - make_interval(secs => :seconds)
                  AND NOT organization_has_overdue_answers(r.organization_id)
                ORDER BY r.id, m.finished_at DESC"""
        ),
        {"seconds": asks.ASK_FRESHNESS.total_seconds()},
    ).all()
    asked = 0
    for row in runs:
        role = asks.open_role(conn, row.role_id)
        if role is not None:
            asked += asks.ask_from_run(conn, role, row.run_id).asked
    return asked


@scheduler.every("hiring.sweep", SWEEP_SECONDS)
def sweep(conn: Connection, payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    expired = _expire(conn)
    queued = _schedule_runs(conn, now)
    resumed = _resume(conn)
    log.event("hiring.sweep", level=logging.INFO, expired=expired, queued=queued, asked=resumed)
