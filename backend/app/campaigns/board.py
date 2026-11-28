"""The campaign board operations (AI_SYSTEM 6.2): the only way a node touches `campaign_steps`. Each function takes the
caller's worker connection (a transaction of the campaign's owner) and is one unit of that transaction, so a step's
output is committed with the artifact it names and a crash leaves either both or neither (ARCHITECTURE 5.9 rule 3).

A step row is opened before the work (`campaign_steps_once` makes a second open the same row), `note` records the ids of
what was made as soon as it exists, and `complete` or `fail` ends it. Step outputs hold ids, counts, codes and validated
text only (INDEX "Step outputs").
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.platform import queue

ENDED = ("done", "failed", "skipped", "cancelled")
WAKE_ATTEMPTS = 20  # assumption: same as the resume job; a wake that finds the thread busy is retried


@dataclass(frozen=True)
class Step:
    id: UUID
    campaign_id: UUID
    step_key: str
    status: str
    due_at: datetime
    output: dict[str, Any]
    waiting_for_approval_id: UUID | None
    error_code: str | None
    finished_at: datetime | None

    @property
    def ended(self) -> bool:
        return self.status in ENDED


_COLUMNS = (
    "id, campaign_id, step_key, status::text AS status, due_at, output, "
    "waiting_for_approval_id, error_code, finished_at"
)


def _step(row: Any) -> Step:
    return Step(
        row.id,
        row.campaign_id,
        row.step_key,
        row.status,
        row.due_at,
        dict(row.output or {}),
        row.waiting_for_approval_id,
        row.error_code,
        row.finished_at,
    )


def open_step(conn: Connection, campaign_id: UUID, step_key: str, agent_role: str) -> Step:
    """Open the step's row, or return the one already there (a re-run node sees its committed work and skips it)."""
    conn.execute(
        text(
            """INSERT INTO campaign_steps (campaign_id, user_id, step_key, agent_role)
               SELECT id, user_id, :key, CAST(:role AS agent_role) FROM campaigns WHERE id = :c
               ON CONFLICT ON CONSTRAINT campaign_steps_once DO NOTHING"""
        ),
        {"c": campaign_id, "key": step_key, "role": agent_role},
    )
    return get_step(conn, campaign_id, step_key)  # type: ignore[return-value]


def get_step(conn: Connection, campaign_id: UUID, step_key: str) -> Step | None:
    row = conn.execute(
        text(f"SELECT {_COLUMNS} FROM campaign_steps WHERE campaign_id = :c AND step_key = :key"),
        {"c": campaign_id, "key": step_key},
    ).one_or_none()
    return None if row is None else _step(row)


def steps_of(conn: Connection, campaign_id: UUID) -> dict[str, Step]:
    rows = conn.execute(text(f"SELECT {_COLUMNS} FROM campaign_steps WHERE campaign_id = :c"), {"c": campaign_id})
    return {row.step_key: _step(row) for row in rows}


def start(conn: Connection, step: Step) -> None:
    """Mark the step running (a node entered); counts the attempt."""
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'running', attempts = attempts + 1,
                      started_at = COALESCE(started_at, now())
               WHERE id = :id AND status NOT IN ('done', 'failed', 'skipped', 'cancelled')"""
        ),
        {"id": step.id},
    )


def note(conn: Connection, step_id: UUID, ids: Mapping[str, Any]) -> None:
    """Merge ids into the step's output, in the transaction that created what they name."""
    conn.execute(
        text("UPDATE campaign_steps SET output = COALESCE(output, '{}'::jsonb) || CAST(:ids AS jsonb) WHERE id = :id"),
        {"id": step_id, "ids": json.dumps(dict(ids), default=str)},
    )


def complete(conn: Connection, step_id: UUID, output: Mapping[str, Any] | None = None) -> None:
    """End the step as done, with its output merged in, in the caller's transaction."""
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'done', finished_at = now(), error_code = NULL,
                      waiting_for_approval_id = NULL,
                      output = (COALESCE(output, '{}'::jsonb) - 'todo') || CAST(:out AS jsonb)
               WHERE id = :id"""
        ),
        {"id": step_id, "out": json.dumps(dict(output or {}), default=str)},
    )


def fail(conn: Connection, step_id: UUID, error_code: str, output: Mapping[str, Any] | None = None) -> None:
    """End the step as failed with a code (a step that cannot succeed by retrying; a transient failure raises)."""
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'failed', finished_at = now(), error_code = :code,
                      output = COALESCE(output, '{}'::jsonb) || CAST(:out AS jsonb)
               WHERE id = :id"""
        ),
        {"id": step_id, "code": error_code, "out": json.dumps(dict(output or {}), default=str)},
    )


def wait_until(
    conn: Connection, step: Step, user_id: UUID, at: datetime, output: Mapping[str, Any] | None = None
) -> None:
    """Set the step waiting for time and enqueue its wake job together (`campaign.wake:{id}:{at}`, run at `at`)."""
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'waiting', due_at = :at, waiting_for_approval_id = NULL,
                      output = (COALESCE(output, '{}'::jsonb) - 'todo') || CAST(:out AS jsonb)
               WHERE id = :id"""
        ),
        {"id": step.id, "at": at, "out": json.dumps(dict(output or {}), default=str)},
    )
    queue.enqueue(
        conn,
        "campaign.wake",
        {"campaign_id": str(step.campaign_id), "due_at": at.isoformat()},
        unique_key=f"campaign.wake:{step.campaign_id}:{at.isoformat()}",
        run_at=at,
        max_attempts=WAKE_ATTEMPTS,
        user_id=user_id,
    )


def wait_for_person(conn: Connection, step_id: UUID, output: Mapping[str, Any]) -> None:
    """Set the step waiting for something the candidate does (a typed address, ...): `waiting`, nothing to wake."""
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'waiting', waiting_for_approval_id = NULL,
                      output = COALESCE(output, '{}'::jsonb) || CAST(:out AS jsonb)
               WHERE id = :id"""
        ),
        {"id": step_id, "out": json.dumps(dict(output), default=str)},
    )


def propose(
    conn: Connection,
    step: Step,
    run_id: UUID,
    *,
    action_type: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
    step_id: str,
    organization_id: UUID | None = None,
) -> guardian.Proposed:
    """Propose one outside action through the Guardian and set the step `waiting` on its approval, in one transaction.
    `step_id` keys the proposal, so a re-run returns the action already made. A blocked proposal leaves the step waiting
    with the action id and no approval: `route` reads the action to decide what comes next."""
    proposed = guardian.propose_action(
        conn,
        run_id,
        action_type=action_type,
        payload=payload,
        provenance=provenance,
        organization_id=organization_id,
        inputs={"step_id": step_id},
    )
    conn.execute(
        text(
            """UPDATE campaign_steps SET status = 'waiting', waiting_for_approval_id = :approval,
                      output = (COALESCE(output, '{}'::jsonb) - 'todo') || CAST(:out AS jsonb)
               WHERE id = :id"""
        ),
        {
            "id": step.id,
            "approval": proposed.approval_id,
            "out": json.dumps({"action_id": proposed.action_id, "approval_id": proposed.approval_id}, default=str),
        },
    )
    return proposed
