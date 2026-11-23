"""The campaigns source of the live stream (API 4.3): the person's open campaigns and their steps.

Snapshot keys `campaigns` and `steps`; events `campaign.stage`, `step.status` and `cost.updated`. Rows are read on the
API login under the caller's id, so row-level security (and the `user_id` filter) keep every other person out.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.platform import stream
from app.platform.llm.gateway import usd
from app.platform.stream import Event

from .stages import PAUSE_REASONS

SLACK_SECONDS = 60  # assumption: a campaign closed this long before the snapshot, or a commit this late, is still read

_CAMPAIGNS = """SELECT id, stage::text AS stage, paused_at, closed_reason::text AS closed_reason, budget_microusd
                FROM campaigns
                WHERE user_id = :u AND (stage <> 'closed' OR id = ANY(CAST(:known AS uuid[]))
                                        OR closed_at >= CAST(:since AS timestamptz))
                ORDER BY created_at, id"""
_STEPS = """SELECT campaign_id, step_key, agent_role::text AS agent_role, status::text AS status, due_at,
                   waiting_for_approval_id
            FROM campaign_steps WHERE user_id = :u AND campaign_id = ANY(CAST(:ids AS uuid[]))
            ORDER BY created_at, id"""


@dataclass
class State:
    since: datetime  # the database's clock at the snapshot, less SLACK_SECONDS
    campaigns: dict[UUID, tuple[str, datetime | None, str | None, str | None, int]] = field(default_factory=dict)
    steps: dict[tuple[UUID, str], tuple[str, datetime, UUID | None]] = field(default_factory=dict)


def _pause_reason(conn: Connection, campaign_id: UUID) -> str | None:
    """Why the campaign is paused: the newest state-change row that carries one of the pause reasons."""
    found = [guardian.last_state_change(conn, campaign_id, code) for code in PAUSE_REASONS]
    newest = max((row for row in found if row is not None), key=lambda row: row["id"], default=None)
    return None if newest is None else str(newest["outcome_code"])


def _read(conn: Connection, user_id: UUID, state: State) -> tuple[Sequence[Any], Sequence[Any]]:
    campaigns = conn.execute(
        text(_CAMPAIGNS), {"u": user_id, "known": list(state.campaigns), "since": state.since}
    ).all()
    steps = conn.execute(text(_STEPS), {"u": user_id, "ids": [row.id for row in campaigns]}).all()
    return campaigns, steps


def _step_data(row: Any) -> dict[str, Any]:
    return {
        "campaign_id": row.campaign_id,
        "step_key": row.step_key,
        "agent_role": row.agent_role,
        "status": row.status,
        "due_at": row.due_at,
        "waiting_for_approval_id": row.waiting_for_approval_id,
    }


def snapshot(conn: Connection, user_id: UUID) -> tuple[dict[str, Any], State]:
    since = conn.execute(text("SELECT now() - make_interval(secs => :s)"), {"s": SLACK_SECONDS}).scalar_one()
    state = State(since)
    campaigns, steps = _read(conn, user_id, state)
    shown = []
    for row in campaigns:
        reason = None if row.paused_at is None else _pause_reason(conn, row.id)
        spent = guardian.spent_microusd(conn, campaign_id=row.id)
        state.campaigns[row.id] = (row.stage, row.paused_at, reason, row.closed_reason, spent)
        if row.stage != "closed":  # a campaign closed a moment ago is tracked, so its close is not announced again
            shown.append(
                {
                    "id": row.id,
                    "stage": row.stage,
                    "paused_at": row.paused_at,
                    "pause_reason": reason,
                    "spent_usd": usd(spent),
                    "budget_usd": usd(row.budget_microusd),
                }
            )
    open_ids = {row.id for row in campaigns if row.stage != "closed"}
    for row in steps:
        state.steps[(row.campaign_id, row.step_key)] = (row.status, row.due_at, row.waiting_for_approval_id)
    return {
        "campaigns": shown,
        "steps": [_step_data(row) for row in steps if row.campaign_id in open_ids],
    }, state


def poll(conn: Connection, user_id: UUID, state: State) -> tuple[list[Event], State]:
    campaigns, steps = _read(conn, user_id, state)
    events: list[Event] = []
    costs: list[Event] = []
    for row in campaigns:
        before = state.campaigns.get(row.id)
        if row.stage == "closed" and before is not None and before[0] == "closed":
            continue  # nothing more to say about it, and no spend query for it
        if before is not None and before[1] == row.paused_at:
            reason = before[2]
        else:
            reason = None if row.paused_at is None else _pause_reason(conn, row.id)
        # ponytail: one spend query per open campaign per poll; group them by campaign if a person has dozens
        spent = guardian.spent_microusd(conn, campaign_id=row.id)
        now = (row.stage, row.paused_at, reason, row.closed_reason, spent)
        if before is None or before[:4] != now[:4]:
            events.append(
                Event(
                    "campaign.stage",
                    {
                        "campaign_id": row.id,
                        "from": None if before is None else before[0],
                        "to": row.stage,
                        "paused_at": row.paused_at,
                        "pause_reason": reason,
                        "closed_reason": row.closed_reason,
                    },
                )
            )
        if (before is None and spent) or (before is not None and before[4] != spent):
            costs.append(
                Event(
                    "cost.updated",
                    {"campaign_id": row.id, "spent_usd": usd(spent), "budget_usd": usd(row.budget_microusd)},
                )
            )
        state.campaigns[row.id] = now
    for row in steps:
        key = (row.campaign_id, row.step_key)
        now_step = (row.status, row.due_at, row.waiting_for_approval_id)
        if state.steps.get(key) != now_step:
            events.append(Event("step.status", _step_data(row)))
            state.steps[key] = now_step
    return events + costs, state


stream.source("campaigns", snapshot=snapshot, poll=poll)
