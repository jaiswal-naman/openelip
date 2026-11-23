"""The one writer of a campaign's stage, closing and pause (INDEX decision 7 and the Stages contract).

Nothing else writes `campaigns.stage`, `closed_at`, `closed_reason` or `paused_at`. Every change is a compare-and-set on
`lock_version` and leaves one `state_change` row in the flight recorder, written by the SQL function record_state_change
(decision 31), so these functions run on the API's connection (the candidate's own pause, resume and cancel) and on the
worker's alike; the connection must act for the campaign's owner (`db.transaction(user_id)` or
`db.transaction(user_id, role="worker")`).

Each change enqueues `campaign.resume` for the campaign's thread unless `wake=False` (the thread itself is the caller).
"""

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.platform import queue

RESUME_ATTEMPTS = 20  # assumption: a lock held by a running thread is retried on the queue's first backoff
PAUSE_REASONS = ("candidate", "budget_exhausted", "scam_suspected")
STAGES = ("researching", "needs_you", "applied", "talking", "interview", "offer", "closed")
CLOSED_REASONS = ("cancelled", "rejected", "no_response", "job_closed", "offer_accepted", "offer_declined")


def _read(conn: Connection, campaign_id: UUID) -> Any:
    row = conn.execute(
        text("SELECT user_id, stage::text AS stage, lock_version, paused_at FROM campaigns WHERE id = :id"),
        {"id": campaign_id},
    ).one_or_none()
    if row is None:
        raise LookupError("no such campaign")
    return row


def _record(conn: Connection, campaign_id: UUID, line: str, code: str, inputs: Mapping[str, Any]) -> None:
    conn.execute(
        text("SELECT record_state_change(:c, :line, :code, CAST(:inputs AS jsonb))"),
        {"c": campaign_id, "line": line, "code": code, "inputs": json.dumps(dict(inputs))},
    )


def enqueue_wake(conn: Connection, campaign_id: UUID, user_id: UUID, cause: str) -> None:
    """Ask the campaign's thread to look again, for this cause (one job per campaign and cause)."""
    queue.enqueue(
        conn,
        "campaign.resume",
        {"campaign_id": str(campaign_id), "cause": cause},
        unique_key=f"campaign.resume:{campaign_id}:{cause}",
        max_attempts=RESUME_ATTEMPTS,
        user_id=user_id,
    )


def move(
    conn: Connection,
    campaign_id: UUID,
    to: str,
    *,
    cause: str,
    closed_reason: str | None = None,
    wake: bool = True,
) -> bool:
    """Move the campaign to stage `to`. False, and nothing written, when it is already there or closed, or when another
    writer changed it first (the lock version moved). Moving to `closed` needs a reason and clears a pause; the state
    change's outcome code is the closed reason, else `cause`."""
    if to not in STAGES or (to == "closed") != (closed_reason is not None):
        raise ValueError("a move to closed needs a closed_reason, and no other move may have one")
    if closed_reason is not None and closed_reason not in CLOSED_REASONS:
        raise ValueError(f"unknown closed_reason {closed_reason!r}")
    row = _read(conn, campaign_id)
    if row.stage in ("closed", to):
        return False
    moved = conn.execute(
        text(
            """UPDATE campaigns SET stage = CAST(:to AS campaign_stage), lock_version = lock_version + 1,
                      closed_at = CASE WHEN :closing THEN now() END,
                      closed_reason = CAST(:reason AS campaign_closed_reason),
                      paused_at = CASE WHEN :closing THEN NULL ELSE paused_at END
               WHERE id = :id AND lock_version = :version AND stage = CAST(:was AS campaign_stage)"""
        ),
        {
            "id": campaign_id,
            "to": to,
            "closing": to == "closed",
            "reason": closed_reason,
            "version": row.lock_version,
            "was": row.stage,
        },
    ).rowcount
    if not moved:
        return False
    _record(
        conn,
        campaign_id,
        f"Stage moved from {row.stage} to {to}.",
        closed_reason or cause,
        {"cause": cause, "from": row.stage, "to": to},
    )
    if wake:
        enqueue_wake(conn, campaign_id, row.user_id, cause)
    return True


def pause(
    conn: Connection,
    campaign_id: UUID,
    reason: str,
    *,
    details: Mapping[str, Any] | None = None,
    wake: bool = True,
) -> bool:
    """Pause the campaign (CMP-07, GRD-07) for `reason` (`candidate`, `budget_exhausted` or `scam_suspected`), keep its
    scheduled follow-ups from being sent, and record why; `details` (the quoted scam signals, for example) go into the
    recorder row. False when it is already paused or closed."""
    if reason not in PAUSE_REASONS:
        raise ValueError(f"unknown pause reason {reason!r}")
    row = _read(conn, campaign_id)
    if row.stage == "closed" or row.paused_at is not None:
        return False
    moved = conn.execute(
        text(
            """UPDATE campaigns SET paused_at = now(), lock_version = lock_version + 1
               WHERE id = :id AND lock_version = :version AND paused_at IS NULL AND stage <> 'closed'"""
        ),
        {"id": campaign_id, "version": row.lock_version},
    ).rowcount
    if not moved:
        return False
    _record(conn, campaign_id, f"Campaign paused: {reason}.", reason, {"reason": reason, **(details or {})})
    guardian.hold(conn, campaign_id)
    if wake:
        enqueue_wake(conn, campaign_id, row.user_id, f"pause.{row.lock_version + 1}")
    return True


def unpause(conn: Connection, campaign_id: UUID, *, by: str, wake: bool = True) -> bool:
    """Let a paused campaign run again and release its held follow-ups with a minute left to undo. `by` says who:
    `candidate`, or `budget_raised`. False when it was not paused."""
    row = _read(conn, campaign_id)
    if row.paused_at is None:
        return False
    moved = conn.execute(
        text(
            """UPDATE campaigns SET paused_at = NULL, lock_version = lock_version + 1
               WHERE id = :id AND lock_version = :version AND paused_at IS NOT NULL"""
        ),
        {"id": campaign_id, "version": row.lock_version},
    ).rowcount
    if not moved:
        return False
    _record(conn, campaign_id, "Campaign resumed.", "unpaused", {"by": by})
    guardian.unhold(conn, campaign_id)
    if wake:
        enqueue_wake(conn, campaign_id, row.user_id, f"unpause.{row.lock_version + 1}")
    return True
