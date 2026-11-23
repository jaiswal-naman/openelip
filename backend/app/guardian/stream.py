"""The Guardian source of the live stream (API 4.3): pending approvals, the flight recorder, approvals and the life of
outside actions.

Snapshot key `pending_approvals`; events `recorder.step` (the entry summary of API 3.16, with `id:` set to the
`agent_actions.id`), `approval.status` and `action.status`. Everything is read on the API login for the caller.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import outbox, stream
from app.platform.llm.gateway import usd
from app.platform.stream import Event

from .recorder import _COST  # the recorder's own cost of an entry, so the summary matches the timeline

ID_SLACK = 1000  # assumption: identities commit out of order by less than this many ids
SLACK_SECONDS = 60  # assumption: an approval committed this late after its request time is still read
FINAL_ACTIONS = ("executed", "blocked", "rejected", "expired", "cancelled", "failed")
FINAL_OUTBOX = (None, "sent", "failed", "cancelled", "unknown")


@dataclass
class State:
    since: datetime  # approvals requested after this are tracked
    floor: int  # recorder rows above this id are looked at (the highest id seen, less ID_SLACK)
    sent: set[int] = field(default_factory=set)  # recorder ids already sent or present at the snapshot
    open: dict[int, tuple[Any, ...]] = field(default_factory=dict)  # outside actions not yet final -> last report
    done: set[int] = field(default_factory=set)  # outside actions reported in a final state, above the floor
    approvals: dict[UUID, str] = field(default_factory=dict)


_ENTRIES = f"""SELECT x.id, x.campaign_id, x.agent_run_id AS run_id, x.seq, r.agent_role::text AS agent_role,
                      x.kind::text AS kind, x.action_type::text AS action_type, x.status::text AS status, x.summary,
                      x.undo_until, x.outcome_code, {_COST} AS cost_microusd
               FROM agent_actions x JOIN agent_runs r ON r.id = x.agent_run_id
               WHERE x.user_id = :u AND (x.id > :floor OR x.id = ANY(CAST(:open AS bigint[])))
               ORDER BY x.id"""
_APPROVALS = """SELECT a.id, a.status::text AS status, x.campaign_id, x.action_type::text AS kind
                FROM approvals a JOIN agent_actions x ON x.id = a.agent_action_id
                WHERE a.user_id = :u AND (a.requested_at >= :since OR a.status = 'pending'
                                      OR a.id = ANY(CAST(:known AS uuid[])))
                ORDER BY a.requested_at, a.id"""


def _approvals(conn: Connection, user_id: UUID, state: State) -> list[Any]:
    # Every pending approval is read, and each one known to be pending stays read, so its decision is seen.
    pending = [i for i, status in state.approvals.items() if status == "pending"]
    return list(conn.execute(text(_APPROVALS), {"u": user_id, "since": state.since, "known": pending}))


def snapshot(conn: Connection, user_id: UUID) -> tuple[dict[str, Any], State]:
    since, top = conn.execute(
        text("SELECT now() - make_interval(secs => :s), (SELECT coalesce(max(id), 0) FROM agent_actions)"),
        {"s": SLACK_SECONDS},
    ).one()
    state = State(since, max(top - ID_SLACK, 0))
    for row in _approvals(conn, user_id, state):
        state.approvals[row.id] = row.status
    pending = conn.execute(
        text("SELECT count(*) FROM approvals WHERE user_id = :u AND status = 'pending'"), {"u": user_id}
    ).scalar_one()
    # Outside actions still going on (not final, or with a message the outbox has not finished) are tracked from now.
    going = [
        row.id
        for row in conn.execute(
            text(
                """SELECT x.id FROM agent_actions x
                   WHERE x.user_id = :u AND x.kind = 'outside_action'
                     AND (x.status::text <> ALL(CAST(:final AS text[]))
                          OR EXISTS (SELECT 1 FROM outbox o WHERE o.agent_action_id = x.id
                                     AND o.status IN ('pending', 'sending')))"""
            ),
            {"u": user_id, "final": list(FINAL_ACTIONS)},
        )
    ]
    rows = conn.execute(text(_ENTRIES), {"u": user_id, "floor": state.floor, "open": going}).all()
    _actions(conn, rows, state, announce=False)  # what stands now is the baseline: nothing is announced
    state.sent.update(row.id for row in rows)
    return {"pending_approvals": pending}, state


def _actions(conn: Connection, rows: Sequence[Any], state: State, *, announce: bool) -> list[Event]:
    """Follow the outside actions among `rows`. An action that reached a final state is reported once and dropped."""
    outside = [row for row in rows if row.kind == "outside_action" and row.id not in state.done]
    states = outbox.action_states(conn, [row.id for row in outside])
    events: list[Event] = []
    for row in outside:
        report = (row.status, row.undo_until, states.get(row.id), row.outcome_code)
        if announce and state.open.get(row.id) != report:
            events.append(
                Event(
                    "action.status",
                    {
                        "action_id": row.id,
                        "type": row.action_type,
                        "status": row.status,
                        "undo_until": row.undo_until,
                        "outbox": report[2],
                        "outcome_code": row.outcome_code,
                    },
                )
            )
        if row.status in FINAL_ACTIONS and report[2] in FINAL_OUTBOX:
            state.open.pop(row.id, None)
            state.done.add(row.id)
        else:
            state.open[row.id] = report
    return events


def poll(conn: Connection, user_id: UUID, state: State) -> tuple[list[Event], State]:
    rows = conn.execute(text(_ENTRIES), {"u": user_id, "floor": state.floor, "open": list(state.open)}).all()
    events: list[Event] = []
    for row in rows:
        if row.id in state.sent:
            continue
        state.sent.add(row.id)
        events.append(
            Event(
                "recorder.step",
                {
                    "campaign_id": row.campaign_id,
                    "id": row.id,
                    "run_id": row.run_id,
                    "seq": row.seq,
                    "agent_role": row.agent_role,
                    "kind": row.kind,
                    "status": row.status,
                    "summary": row.summary,
                    "cost_usd": usd(int(row.cost_microusd)),
                },
                id=row.id,
            )
        )
    for approval in _approvals(conn, user_id, state):
        if state.approvals.get(approval.id) != approval.status:
            state.approvals[approval.id] = approval.status
            events.append(
                Event(
                    "approval.status",
                    {
                        "approval_id": approval.id,
                        "campaign_id": approval.campaign_id,
                        "kind": approval.kind,
                        "status": approval.status,
                    },
                )
            )
    events.extend(_actions(conn, rows, state, announce=True))
    top = max((row.id for row in rows), default=0)
    if top - ID_SLACK > state.floor:  # prune: ids below the new line are never read again
        state.floor = top - ID_SLACK
        state.sent = {i for i in state.sent if i > state.floor}
        state.done = {i for i in state.done if i > state.floor}
    return events, state


stream.source("guardian", snapshot=snapshot, poll=poll)
