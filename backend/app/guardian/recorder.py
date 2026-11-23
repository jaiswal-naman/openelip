"""The flight recorder's writer and readers (GRD-03; DATA_MODEL 4.6 and 6.5).

Every step an agent takes is one `agent_actions` row of one `agent_runs` row, numbered by `seq` within the run. The
writers
here run on a worker connection (the API login cannot insert into agent_actions; the candidate's own state changes go
through the SQL function record_state_change). A row holds ids, counts, codes and short code-written lines, never raw
resume, chat or page text.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

NON_OUTSIDE_KINDS = ("model_call", "tool_call")


def open_run(
    conn: Connection,
    user_id: UUID,
    campaign_id: UUID | None,
    agent_role: str,
    config_version: str,
    *,
    campaign_step_id: UUID | None = None,
) -> UUID:
    """Start one execution of one role. `config_version` names the prompt and tool configuration, for replay."""
    run_id: UUID = conn.execute(
        text(
            """INSERT INTO agent_runs (user_id, campaign_id, campaign_step_id, agent_role, config_version)
               VALUES (:u, :c, :s, CAST(:role AS agent_role), :v) RETURNING id"""
        ),
        {"u": user_id, "c": campaign_id, "s": campaign_step_id, "role": agent_role, "v": config_version},
    ).scalar_one()
    return run_id


def close_run(conn: Connection, run_id: UUID, *, status: str = "succeeded", error_code: str | None = None) -> None:
    conn.execute(
        text(
            """UPDATE agent_runs SET status = CAST(:s AS agent_run_status), error_code = :e, finished_at = now()
               WHERE id = :id AND status = 'running'"""
        ),
        {"id": run_id, "s": status, "e": error_code},
    )


def _lock_run(conn: Connection, run_id: UUID) -> Any:
    row = conn.execute(
        text("SELECT id, user_id, campaign_id FROM agent_runs WHERE id = :id FOR UPDATE"), {"id": run_id}
    ).one_or_none()
    if row is None:
        raise LookupError("no such run")
    return row


def next_seq(conn: Connection, run_id: UUID) -> int:
    """The next order number of the run. The run row is locked first, so two writers never take the same number; the
    lock
    lasts until the caller's transaction ends."""
    row = _lock_run(conn, run_id)
    return int(
        conn.execute(
            text("SELECT COALESCE(max(seq), 0) + 1 FROM agent_actions WHERE agent_run_id = :r"), {"r": row.id}
        ).scalar_one()
    )


def record(
    conn: Connection,
    run_id: UUID,
    *,
    kind: str,
    summary: str,
    inputs: Mapping[str, Any] | None = None,
    evidence: Sequence[Any] = (),
    outcome_code: str | None = None,
) -> int:
    """One model call or tool call of the run; its status is `recorded`. A model call carries `llm_call_ids` in
    `inputs`.
    State changes go through record_state_change and outside actions through approvals.propose_action."""
    if kind not in NON_OUTSIDE_KINDS:
        raise ValueError(f"record writes {NON_OUTSIDE_KINDS}, not {kind!r}")
    run = _lock_run(conn, run_id)
    action_id: int = conn.execute(
        text(
            """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, status, summary, inputs,
                                          evidence, outcome_code)
               VALUES (:r, :u, :c, (SELECT COALESCE(max(seq), 0) + 1 FROM agent_actions WHERE agent_run_id = :r),
                       CAST(:kind AS action_kind), 'recorded', :summary, CAST(:inputs AS jsonb),
                       CAST(:evidence AS jsonb), :outcome)
               RETURNING id"""
        ),
        {
            "r": run.id,
            "u": run.user_id,
            "c": run.campaign_id,
            "kind": kind,
            "summary": summary[:200],
            "inputs": json.dumps(dict(inputs or {})),
            "evidence": json.dumps(list(evidence)),
            "outcome": outcome_code,
        },
    ).scalar_one()
    return action_id


def last_state_change(conn: Connection, campaign_id: UUID, outcome_code: str) -> dict[str, Any] | None:
    """The newest state-change row of the campaign with this outcome code (a pause reason, a cause), or None."""
    row = (
        conn.execute(
            text(
                """SELECT id, created_at, summary, outcome_code, inputs FROM agent_actions
                   WHERE campaign_id = :c AND kind = 'state_change' AND outcome_code = :code
                   ORDER BY id DESC LIMIT 1"""
            ),
            {"c": campaign_id, "code": outcome_code},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else dict(row)


# Cost of an entry: the llm_calls its `inputs.llm_call_ids` names (INDEX decision 9), in micro-dollars.
_COST = """COALESCE((SELECT sum(l.cost_microusd) FROM llm_calls l
          WHERE l.id IN (SELECT CAST(e AS bigint) FROM jsonb_array_elements_text(
                           CASE WHEN jsonb_typeof(x.inputs -> 'llm_call_ids') = 'array' THEN x.inputs -> 'llm_call_ids'
                                ELSE '[]'::jsonb END) AS e)), 0)"""


def timeline(
    conn: Connection,
    user_id: UUID,
    *,
    campaign_id: UUID | None = None,
    run_id: UUID | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """The entries of a campaign or a run in order (DATA_MODEL 6.5): when, which role, kind, type, status, summary, each
    Guardian check, the approval, the outcome and the cost. Only the person's own entries appear. Pass one of
    `campaign_id` and `run_id`."""
    if (campaign_id is None) == (run_id is None):
        raise ValueError("pass campaign_id or run_id")
    rows = conn.execute(
        text(
            f"""SELECT x.id, x.agent_run_id AS run_id, x.seq, x.created_at, r.agent_role::text AS agent_role,
                       x.kind::text AS kind, x.action_type::text AS action_type, x.status::text AS status, x.summary,
                       (SELECT jsonb_agg(jsonb_build_object('check', g.check_kind, 'passed', g.passed,
                                                            'reason', g.reason_code) ORDER BY g.id)
                          FROM guardian_checks g WHERE g.agent_action_id = x.id) AS guardian,
                       a.status::text AS approval, a.decided_at, x.executed_at, x.outcome_code,
                       {_COST} AS cost_microusd, jsonb_array_length(x.evidence) AS evidence_count
                FROM agent_actions x
                JOIN agent_runs r ON r.id = x.agent_run_id
                LEFT JOIN approvals a ON a.agent_action_id = x.id
                WHERE x.user_id = :u AND (x.campaign_id = :c OR x.agent_run_id = :r)
                ORDER BY x.created_at, x.agent_run_id, x.seq
                LIMIT :limit"""
        ),
        {"u": user_id, "c": campaign_id, "r": run_id, "limit": limit},
    ).mappings()
    return [dict(row) for row in rows]


def entry(conn: Connection, user_id: UUID, entry_id: int) -> dict[str, Any] | None:
    """One entry in full: inputs, evidence, payload, the policy decision and version, every check with its reason and
    details, the approval, the outcome, the model calls it names and their cost. None for another person's entry."""
    row = (
        conn.execute(
            text(
                f"""SELECT x.id, x.agent_run_id AS run_id, x.seq, x.created_at, r.agent_role::text AS agent_role,
                           r.config_version, x.campaign_id, x.kind::text AS kind, x.action_type::text AS action_type,
                           x.status::text AS status, x.summary, x.inputs, x.evidence, x.payload,
                           x.policy_decision::text AS policy_decision, x.policy_version, x.undo_until,
                           x.executed_at, x.outcome_code, {_COST} AS cost_microusd
                    FROM agent_actions x JOIN agent_runs r ON r.id = x.agent_run_id
                    WHERE x.id = :id AND x.user_id = :u"""
            ),
            {"id": entry_id, "u": user_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    found = dict(row)
    found["checks"] = [
        dict(c)
        for c in conn.execute(
            text(
                """SELECT id, check_kind::text AS check, passed, reason_code, details, guardian_version, checked_at
                   FROM guardian_checks WHERE agent_action_id = :id ORDER BY id"""
            ),
            {"id": entry_id},
        ).mappings()
    ]
    approval = (
        conn.execute(
            text(
                """SELECT id, status::text AS status, requested_at, expires_at, decided_at, decided_via, note
                   FROM approvals WHERE agent_action_id = :id"""
            ),
            {"id": entry_id},
        )
        .mappings()
        .one_or_none()
    )
    found["approval"] = None if approval is None else dict(approval)
    ids = [int(i) for i in (found["inputs"] or {}).get("llm_call_ids", []) if isinstance(i, int)]
    found["model_calls"] = [
        dict(c)
        for c in conn.execute(
            text(
                """SELECT id, task, prompt_version, outcome::text AS outcome, input_tokens, output_tokens, cost_microusd
                   FROM llm_calls WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"""
            ),
            {"ids": ids},
        ).mappings()
    ]
    return found
