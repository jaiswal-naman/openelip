"""The Guardian's queue handlers: guardian.release, and the sweeps guardian.expire and guardian.settle. The worker
imports this module, so the handlers exist before the first job is claimed, and so does the follow_up_v1 email
template (importing sender registers it with the relay)."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from app.platform import log, queue, scheduler

from . import approvals, sender  # noqa: F401  (sender registers follow_up_v1)

SWEEP_SECONDS = 60


@queue.handler("guardian.release")
def release(conn: Connection, payload: dict[str, Any]) -> None:
    """Run both keys again for an approved action and schedule or block it. It opens its own worker transaction (as the
    person), so the job's connection is not used. Running it twice changes nothing."""
    approval_id = UUID(payload["approval_id"])
    outcome = approvals.release(approval_id, payload.get("edits"))
    log.event("guardian.release", approval_id=approval_id, outcome=outcome)


@scheduler.every("guardian.expire", SWEEP_SECONDS)
def expire(conn: Connection, payload: dict[str, Any]) -> None:
    count = approvals.expire_due(conn)
    if count:
        log.event("guardian.expired", count=count)


@scheduler.every("guardian.settle", SWEEP_SECONDS)
def settle(conn: Connection, payload: dict[str, Any]) -> None:
    count = approvals.settle(conn)
    if count:
        log.event("guardian.settled", count=count)
