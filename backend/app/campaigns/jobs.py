"""The campaign queue handlers: `campaign.resume`, `campaign.wake` and `graph.delete` (ARCHITECTURE 5.9 rules 7, 8, 11).
Importing this module is how the worker registers them. Only these jobs run a campaign thread.

A handler takes the thread's advisory lock, then starts, resumes or continues the thread (`guardian.runtime.drive`). A
thread that is busy raises ThreadBusy, so the queue retries the job on its first 30-second backoff (INDEX decision 25).
Each job is safe to run twice: the graph reads the tables, and every effect it has is guarded by a unique key.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.guardian import runtime
from app.platform import db, log, queue

from . import graph
from .nodes import thread_id


def _stage_of(campaign_id: UUID) -> str | None:
    with db.transaction(role="worker") as conn:
        return conn.execute(
            text("SELECT stage::text FROM campaigns WHERE id = :id"), {"id": campaign_id}
        ).scalar_one_or_none()


def run_thread(campaign_id: UUID, cause: str) -> str:
    """Drive the campaign's thread for this cause. A campaign that is gone or closed starts no new thread (a closed one
    with a parked thread still resumes, so its last `route` ends the thread and deletes it)."""
    stage = _stage_of(campaign_id)
    if stage is None:
        return "gone"
    start = None if stage == "closed" else graph.start_state(campaign_id)
    outcome = runtime.drive(thread_id(campaign_id), graph.compiled(), cause=cause, start_state=start)
    log.event("campaign.thread", campaign_id=campaign_id, outcome=outcome)
    return outcome


@queue.handler("campaign.resume")
def resume(conn: Connection, payload: dict[str, Any]) -> None:
    run_thread(UUID(payload["campaign_id"]), str(payload["cause"]))


@queue.handler("campaign.wake")
def wake(conn: Connection, payload: dict[str, Any]) -> None:
    run_thread(UUID(payload["campaign_id"]), f"wake.{payload['due_at']}")


@queue.handler("graph.delete")
def delete(conn: Connection, payload: dict[str, Any]) -> None:
    """Remove a finished thread's checkpoints. Under the thread's lock, so a run still writing is waited for."""
    thread = str(payload["thread_id"])
    with runtime.thread_lock(thread):
        runtime.saver().delete_thread(thread)
    log.event("graph.deleted")
