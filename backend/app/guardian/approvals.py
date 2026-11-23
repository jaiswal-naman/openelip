"""Proposals, approvals, release, undo, expiry and cancellation of outside actions (AI_SYSTEM 7.2 and 7.5; GRD-01 to
GRD-04).

Who writes what (INDEX decision 4). The worker inserts `agent_actions`, `approvals` and `guardian_checks` rows
(propose_action, release, expire_due); the internet-facing API login can only move a row forward: decide records the
candidate's yes or no and enqueues `guardian.release`, undo cancels, cancel_open and hold/unhold run on the
connection the
caller gives. The API can therefore never write a passing check; the worker reruns both keys in `release`.

Every status change that matters publishes `guardian.action_changed` (cause `a{id}.{status}`) when the action
belongs to a
campaign, so the campaign's thread wakes.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.platform import db, events, log, outbox, queue
from app.platform.errors import ApiError
from app.platform.llm import attributed

from . import autonomy, checks, policy

APPROVAL_TTL = timedelta(days=7)  # assumption: API 3.6's example; an unanswered request then expires
RELEASE_QUEUE = "interactive"
FOLLOW_UP_TEMPLATE = "follow_up_v1"
HELD_MINIMUM = timedelta(minutes=1)  # a resumed follow-up keeps at least this much undo time (decision 12)
EDITABLE_FIELDS: dict[str, list[str]] = {
    "submit_application": ["answers[].answer", "cover_note"],
    "send_follow_up": ["subject", "body"],
}
MAX_EDIT_LENGTH = 5000  # assumption: characters in one edited field


def payload_hash(payload: Mapping[str, Any]) -> str:
    """sha256 hex of the payload as JSON with sorted keys and no spaces: what the candidate saw is what they approve."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Proposed:
    action_id: int
    status: str
    approval_id: UUID | None
    failed_codes: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    approval_id: UUID
    approval_status: str
    note: str | None
    decided_at: datetime
    action_id: int
    action_status: str
    release: str | None  # "pending" until the worker has run both keys again; None for a rejection


@dataclass(frozen=True)
class UndoResult:
    action_id: int
    status: str
    outbox_cancelled: int


def _conflict(state: str, message: str = "The state of this action does not allow that.") -> ApiError:
    return ApiError(409, "conflict", message, {"state": state})


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such resource.")


def _changed(conn: Connection, action_id: int, campaign_id: UUID | None, user_id: UUID, status: str) -> None:
    """Tell the campaign's thread that an action moved (only an action that belongs to a campaign)."""
    if campaign_id is not None:
        cause = f"a{action_id}.{status}"
        events.publish(
            conn,
            "guardian.action_changed",
            {"campaign_id": str(campaign_id), "cause": cause, "agent_action_id": action_id, "status": status},
            key=f"{campaign_id}:{cause}",
            user_id=user_id,
        )


@contextmanager
def _two_key_errors(conn: Connection, action_id: int) -> Iterator[None]:
    """Run one statement in a savepoint; a refusal by the two-key trigger becomes `409 conflict` with the action's
    state."""
    try:
        with conn.begin_nested():
            yield
    except DBAPIError as error:
        if "two_key" not in str(error.orig) and "agent action cannot move" not in str(error.orig):
            raise
        state = conn.execute(
            text("SELECT status::text FROM agent_actions WHERE id = :id"), {"id": action_id}
        ).scalar_one_or_none()
        raise _conflict(state or "unknown") from None


# ---- evaluating both keys ------------------------------------------------------------------------------------------


def _evidence(provenance: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for sentence in provenance.get("sentences") or []:
        for fact_id in sentence.get("fact_ids", ()):
            found[f"f:{fact_id}"] = {"fact_id": str(fact_id)}
        for index in sentence.get("claim_indexes", ()):
            found[f"d:{index}"] = {"dossier_id": provenance.get("dossier_id"), "claim_index": int(index)}
    return list(found.values())


def _decide(conn: Connection, gate: policy.SubjectGate, action_type: str) -> policy.PolicyResult:
    """Key one, with the dial read now."""
    return policy.decide(action_type, dial=autonomy.level_of(conn, gate.user_id, action_type), gate=gate)


def _run_checks(
    conn: Connection,
    action_id: int,
    run_id: UUID,
    gate: policy.SubjectGate,
    action_type: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> list[checks.CheckResult]:
    """Key two on the payload as it stands, with the six check rows written. The model call of the citation check is
    charged to the campaign."""
    inp = checks.gather(conn, action_type, payload, provenance, gate, action_id=action_id)
    with attributed(campaign_id=gate.id, agent_run_id=run_id):
        results = checks.run(inp, principal=gate.user_id)
    for check in results:
        conn.execute(
            text(
                """INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, reason_code, details,
                                                guardian_version)
                   VALUES (:a, :u, CAST(:kind AS guardian_check_kind), :passed, :reason, CAST(:details AS jsonb), :v)"""
            ),
            {
                "a": action_id,
                "u": gate.user_id,
                "kind": check.kind,
                "passed": check.passed,
                "reason": check.reason_code,
                "details": json.dumps(check.details),
                "v": checks.GUARDIAN_VERSION,
            },
        )
    return results


def _first_reason(result: policy.PolicyResult, results: Sequence[checks.CheckResult]) -> str | None:
    if result.decision == "deny":
        return result.reason_code
    codes = checks.failed(results)
    return codes[0] if codes else None


def _move(conn: Connection, action_id: int, status: str, *, outcome_code: str | None = None) -> None:
    conn.execute(
        text(
            """UPDATE agent_actions SET status = CAST(:s AS action_status), outcome_code = COALESCE(:o, outcome_code)
               WHERE id = :id"""
        ),
        {"id": action_id, "s": status, "o": outcome_code},
    )


def _block(conn: Connection, row: Any, reason: str) -> None:
    _move(conn, row.id, "blocked", outcome_code=reason)
    _changed(conn, row.id, row.campaign_id, row.user_id, "blocked")
    log.event("guardian.blocked", action_id=row.id, kind=row.action_type, outcome=reason)


def _schedule(conn: Connection, row: Any) -> None:
    """Both keys have passed and the candidate approved: a follow-up becomes scheduled with its undo window and one
    outbox
    row; a packet becomes scheduled and nothing is queued (the candidate submits it themselves)."""
    if row.action_type == "send_follow_up":
        undo_until: datetime = conn.execute(
            text(
                """UPDATE agent_actions SET status = 'scheduled',
                          undo_until = clock_timestamp() + make_interval(mins => u.undo_window_minutes)
                   FROM users u WHERE agent_actions.id = :id AND u.id = agent_actions.user_id
                   RETURNING agent_actions.undo_until"""
            ),
            {"id": row.id},
        ).scalar_one()
        outbox.add(
            conn,
            kind=FOLLOW_UP_TEMPLATE,
            dedupe_key=f"action:{row.id}",
            user_id=row.user_id,
            payload={"agent_action_id": row.id},
            not_before=undo_until,
            agent_action_id=row.id,
        )
    else:
        _move(conn, row.id, "scheduled")
    _changed(conn, row.id, row.campaign_id, row.user_id, "scheduled")
    log.event("guardian.scheduled", action_id=row.id, kind=row.action_type)


# ---- proposing -----------------------------------------------------------------------------------------------------


def _insert_action(
    conn: Connection,
    run: Any,
    *,
    action_type: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
    decision: str,
    organization_id: UUID | None,
    inputs: Mapping[str, Any],
    summary: str,
) -> int:
    stored_inputs = {**inputs, "provenance": dict(provenance)}
    action_id: int = conn.execute(
        text(
            """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, action_type, status, summary,
                                          inputs, evidence, payload, organization_id, policy_decision, policy_version)
               VALUES (:r, :u, :c, (SELECT COALESCE(max(seq), 0) + 1 FROM agent_actions WHERE agent_run_id = :r),
                       'outside_action', CAST(:type AS action_type), 'proposed', :summary, CAST(:inputs AS jsonb),
                       CAST(:evidence AS jsonb), CAST(:payload AS jsonb), :org, CAST(:decision AS policy_decision), :v)
               RETURNING id"""
        ),
        {
            "r": run.id,
            "u": run.user_id,
            "c": run.campaign_id,
            "type": action_type,
            "summary": summary[:200],
            "inputs": json.dumps(stored_inputs),
            "evidence": json.dumps(_evidence(provenance)),
            "payload": json.dumps(dict(payload)),
            "org": organization_id,
            "decision": decision,
            "v": policy.POLICY_VERSION,
        },
    ).scalar_one()
    return action_id


def _gate_of(conn: Connection, run: Any) -> policy.SubjectGate:
    gate = policy.read_gate(conn, "campaign", run.campaign_id) if run.campaign_id else None
    if gate is None:
        raise LookupError("the run has no campaign the Guardian can read")
    return gate


def propose_action(
    conn: Connection,
    run_id: UUID,
    *,
    action_type: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
    organization_id: UUID | None = None,
    inputs: Mapping[str, Any] | None = None,
    summary: str | None = None,
) -> Proposed:
    """Record one proposed outside action, run both keys on it and leave it blocked or waiting for the candidate.
    Runs on a
    worker connection. Pass the step id in `inputs` (`{"step_id": ...}`): proposing again for the same step returns the
    action already made."""
    run = conn.execute(
        text("SELECT id, user_id, campaign_id FROM agent_runs WHERE id = :id FOR UPDATE"), {"id": run_id}
    ).one()
    step_id = (inputs or {}).get("step_id")
    if step_id is not None:
        found = conn.execute(
            text(
                """SELECT a.id, a.status::text AS status, p.id AS approval_id FROM agent_actions a
                   LEFT JOIN approvals p ON p.agent_action_id = a.id
                   WHERE a.agent_run_id = :r AND a.kind = 'outside_action' AND a.inputs ->> 'step_id' = :step
                     AND a.action_type = CAST(:type AS action_type) AND a.inputs ->> 'replaces' IS NULL
                   ORDER BY a.id LIMIT 1"""
            ),
            {"r": run_id, "step": str(step_id), "type": action_type},
        ).one_or_none()
        if found is not None:
            return Proposed(found.id, found.status, found.approval_id, ())
    gate = _gate_of(conn, run)
    result = _decide(conn, gate, action_type)
    if result.decision == "allow":
        raise RuntimeError("an allow decision does not exist before the Twin (Phase 9)")
    action_id = _insert_action(
        conn,
        run,
        action_type=action_type,
        payload=payload,
        provenance=provenance,
        decision=result.decision,
        organization_id=organization_id,
        inputs=inputs or {},
        summary=summary or f"Proposed {action_type.replace('_', ' ')}.",
    )
    results = _run_checks(conn, action_id, run_id, gate, action_type, payload, provenance)
    row = conn.execute(
        text("SELECT id, user_id, campaign_id, action_type::text AS action_type FROM agent_actions WHERE id = :id"),
        {"id": action_id},
    ).one()
    reason = _first_reason(result, results)
    if reason is not None:
        _block(conn, row, reason)
        return Proposed(action_id, "blocked", None, tuple(checks.failed(results)))
    approval_id: UUID = conn.execute(
        text(
            """INSERT INTO approvals (user_id, agent_action_id, expires_at)
               VALUES (:u, :a, now() + :ttl) RETURNING id"""
        ),
        {"u": run.user_id, "a": action_id, "ttl": APPROVAL_TTL},
    ).scalar_one()
    _move(conn, action_id, "awaiting_approval")
    _changed(conn, action_id, run.campaign_id, run.user_id, "awaiting_approval")
    return Proposed(action_id, "awaiting_approval", approval_id, ())


# ---- the candidate's decision (the API login) ----------------------------------------------------------------------


def _invalid(field: str, issue: str) -> ApiError:
    return ApiError(
        422, "validation_failed", "One or more fields are invalid.", {"fields": [{"field": field, "issue": issue}]}
    )


def apply_edits(
    action_type: str, payload: Mapping[str, Any], edits: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """The payload with the candidate's edits and the names of the fields they wrote (the provenance names). Only the
    fields of EDITABLE_FIELDS may change: a follow-up's subject and body; a packet's cover note and the `answer` of an
    answer given as `{"question_id", "answer"}`. Anything else is `422 validation_failed`."""
    allowed = {"send_follow_up": {"subject", "body"}, "submit_application": {"cover_note", "answers"}}.get(action_type)
    if allowed is None:
        raise _invalid("edits", "not_editable")
    for name in edits:
        if name not in allowed:
            raise _invalid(name, "not_editable")
    new = json.loads(json.dumps(dict(payload)))  # a deep copy: the stored payload is never touched
    written: list[str] = []
    for name in ("subject", "body", "cover_note"):
        if name in edits:
            value = edits[name]
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_EDIT_LENGTH:
                raise _invalid(name, "invalid")
            if name == "subject" and ("\n" in value or "\r" in value):
                raise _invalid(name, "no_line_breaks")
            new[name] = value.strip()
            written.append(name)
    if "answers" in edits:
        by_id = {str(a.get("question_id")): a for a in new.get("answers") or []}
        items = edits["answers"]
        if not isinstance(items, list):
            raise _invalid("answers", "invalid")
        for item in items:
            question, value = str(item.get("question_id")) if isinstance(item, dict) else "", (item or {}).get("answer")
            if question not in by_id:
                raise _invalid("answers", "unknown_question")
            if not isinstance(value, str) or len(value) > MAX_EDIT_LENGTH:
                raise _invalid("answers", "invalid")
            by_id[question]["answer"] = value.strip()
            written.append(f"answers.{question}")
    return new, written


def decide(
    conn: Connection,
    user_id: UUID,
    approval_id: UUID,
    *,
    approve: bool,
    payload_hash: str,
    edits: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> Decision:
    """The candidate's yes or no, on their own connection. Approving records the yes and enqueues
    `guardian.release`; the
    worker runs both keys again and schedules or blocks (the API login cannot write a check). Approving with edits
    rejects this approval with the note `edited`; the worker then makes the replacement action with the edits."""
    row = conn.execute(
        text(
            """SELECT p.id, p.status::text AS status, p.expires_at <= now() AS is_expired, a.id AS action_id,
                      a.campaign_id, a.action_type::text AS action_type, a.payload, a.status::text AS action_status
               FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
               WHERE p.id = :id AND p.user_id = :u FOR UPDATE OF p"""
        ),
        {"id": approval_id, "u": user_id},
    ).one_or_none()
    if row is None:
        raise _not_found()
    if row.status != "pending":
        raise ApiError(409, "already_resolved", "This request was already answered.", {"status": row.status})
    if row.is_expired:
        raise _conflict("expired", "This request expired.")
    current = payload_hash_of(row.payload)
    if payload_hash != current:
        raise ApiError(409, "preview_changed", "The draft changed after you saw it.", {"payload_hash": current})
    if approve:
        gate = policy.read_gate(conn, "campaign", row.campaign_id) if row.campaign_id else None
        if row.campaign_id and (gate is None or gate.closed):
            raise _conflict("closed", "This campaign is closed.")
        if gate is not None and gate.paused_reason is not None:
            raise _conflict("paused", "This campaign is paused.")
        if edits:
            apply_edits(row.action_type, row.payload, edits)  # refuses a bad edit before anything is written
    edited = approve and bool(edits)
    approval_status = "approved" if approve and not edited else "rejected"
    stored_note = "edited" if edited else note
    decided_at: datetime = conn.execute(
        text(
            """UPDATE approvals SET status = CAST(:s AS approval_status), decided_at = now(), decided_via = 'app',
                      note = :note WHERE id = :id RETURNING decided_at"""
        ),
        {"id": approval_id, "s": approval_status, "note": stored_note},
    ).scalar_one()
    if approval_status == "rejected":
        _move(conn, row.action_id, "rejected", outcome_code="edited" if edited else "rejected")
        _changed(conn, row.action_id, row.campaign_id, user_id, "rejected")
    if approve:
        queue.enqueue(
            conn,
            "guardian.release",
            {"approval_id": str(approval_id), "edits": dict(edits) if edits else None},
            unique_key=f"guardian.release:{approval_id}",
            queue=RELEASE_QUEUE,
            max_attempts=8,
            user_id=user_id,
        )
    action_status = "rejected" if approval_status == "rejected" else row.action_status
    return Decision(
        approval_id,
        approval_status,
        stored_note,
        decided_at,
        row.action_id,
        action_status,
        "pending" if approve else None,
    )


def payload_hash_of(payload: Mapping[str, Any] | None) -> str:
    return payload_hash(payload or {})


# ---- release (the worker) ------------------------------------------------------------------------------------------


def release(approval_id: UUID, edits: Mapping[str, Any] | None = None) -> str:
    """The worker's half of an approval: run both keys again and schedule the action or block it. Returns `scheduled`,
    `blocked` or `noop` (nothing to do: already released, or the approval is not an approved one). Running it twice
    changes nothing the second time."""
    with db.transaction(role="worker") as conn:
        owner = conn.execute(
            text("SELECT user_id FROM approvals WHERE id = :id"), {"id": approval_id}
        ).scalar_one_or_none()
    if owner is None:
        return "noop"
    with db.transaction(owner, role="worker") as conn:
        return _release(conn, approval_id, edits)


def _release(conn: Connection, approval_id: UUID, edits: Mapping[str, Any] | None) -> str:
    row = conn.execute(
        text(
            """SELECT p.status::text AS approval_status, p.note AS approval_note, a.id, a.user_id, a.campaign_id,
                      a.agent_run_id, a.action_type::text AS action_type, a.status::text AS status, a.payload, a.inputs,
                      a.organization_id
               FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
               WHERE p.id = :id FOR UPDATE OF a"""
        ),
        {"id": approval_id},
    ).one_or_none()
    if row is None:
        return "noop"
    gate = policy.read_gate(conn, "campaign", row.campaign_id) if row.campaign_id else None
    if gate is None:
        return "noop"
    provenance = (row.inputs or {}).get("provenance") or {}
    if edits:
        return _release_edited(conn, row, gate, provenance, edits)
    if row.approval_status != "approved" or row.status != "awaiting_approval":
        return "noop"
    result = _decide(conn, gate, row.action_type)
    results = _run_checks(conn, row.id, row.agent_run_id, gate, row.action_type, row.payload, provenance)
    reason = _first_reason(result, results)
    if reason is not None:
        _block(conn, row, reason)
        return "blocked"
    _schedule(conn, row)
    return "scheduled"


def _release_edited(
    conn: Connection, original: Any, gate: policy.SubjectGate, provenance: Mapping[str, Any], edits: Mapping[str, Any]
) -> str:
    if original.approval_status != "rejected" or original.approval_note != "edited" or original.status != "rejected":
        return "noop"
    made = conn.execute(
        text("SELECT 1 FROM agent_actions WHERE agent_run_id = :r AND inputs ->> 'replaces' = :orig"),
        {"r": original.agent_run_id, "orig": str(original.id)},
    ).first()
    if made is not None:
        return "noop"
    try:
        payload, written = apply_edits(original.action_type, original.payload, edits)
    except ApiError:
        log.event("guardian.edit_invalid", action_id=original.id)
        return "noop"
    fields = {**(provenance.get("fields") or {}), **{name: "candidate" for name in written}}
    new_provenance = {
        **provenance,
        "fields": fields,
        "sentences": [s for s in provenance.get("sentences") or [] if s.get("field") not in written],
    }
    run = conn.execute(
        text("SELECT id, user_id, campaign_id FROM agent_runs WHERE id = :id"), {"id": original.agent_run_id}
    ).one()
    result = _decide(conn, gate, original.action_type)
    action_id = _insert_action(
        conn,
        run,
        action_type=original.action_type,
        payload=payload,
        provenance=new_provenance,
        decision=result.decision,
        organization_id=original.organization_id,
        inputs={"replaces": original.id},
        summary=f"{original.action_type.replace('_', ' ').capitalize()} edited by you.".capitalize(),
    )
    results = _run_checks(conn, action_id, run.id, gate, original.action_type, payload, new_provenance)
    conn.execute(
        text("""INSERT INTO approvals (user_id, agent_action_id, expires_at) VALUES (:u, :a, now() + :ttl)"""),
        {"u": run.user_id, "a": action_id, "ttl": APPROVAL_TTL},
    )
    conn.execute(
        text(
            """UPDATE approvals SET status = 'approved', decided_at = now(), decided_via = 'app',
                      note = 'approved with edits'
               WHERE agent_action_id = :a"""
        ),
        {"a": action_id},
    )
    row = conn.execute(
        text("SELECT id, user_id, campaign_id, action_type::text AS action_type FROM agent_actions WHERE id = :id"),
        {"id": action_id},
    ).one()
    reason = _first_reason(result, results)
    if reason is not None:
        _block(conn, row, reason)
        return "blocked"
    _schedule(conn, row)
    return "scheduled"


# ---- undo, expiry, cancellation, holds -----------------------------------------------------------------------------


def undo(conn: Connection, user_id: UUID, action_id: int) -> UndoResult:
    """Cancel a scheduled follow-up and its message while the message is still waiting for its time (GRD-04). The API's
    own connection does it. Once the relay has claimed the message the answer is `409 conflict` with the state:
    `sending`, `sent` or `executed`."""
    row = conn.execute(
        text(
            """SELECT id, campaign_id, action_type::text AS action_type, status::text AS status FROM agent_actions
               WHERE id = :id AND user_id = :u FOR UPDATE"""
        ),
        {"id": action_id, "u": user_id},
    ).one_or_none()
    if row is None:
        raise _not_found()
    if row.action_type != "send_follow_up" or row.status != "scheduled":
        raise _conflict(row.status)
    message = conn.execute(
        text("SELECT status::text AS status, not_before > now() AS waiting FROM outbox WHERE agent_action_id = :id"),
        {"id": action_id},
    ).one_or_none()
    if message is None or message.status != "pending" or not message.waiting:
        state = "sending" if message is None or message.status in ("pending", "sending", "unknown") else message.status
        raise _conflict(state)
    cancelled = outbox.cancel_for_actions(conn, [action_id], "undone")
    if not cancelled:
        raise _conflict("sending")
    with _two_key_errors(conn, action_id):
        _move(conn, action_id, "cancelled", outcome_code="undone")
    _changed(conn, action_id, row.campaign_id, user_id, "cancelled")
    return UndoResult(action_id, "cancelled", len(cancelled))


def expire_due(conn: Connection) -> int:
    """The sweep: pending approvals past `expires_at` become `expired` and so do their actions. Returns how many."""
    rows = conn.execute(
        text(
            """UPDATE approvals SET status = 'expired', decided_at = now()
               WHERE status = 'pending' AND expires_at <= now() RETURNING agent_action_id"""
        )
    ).all()
    for found in rows:
        moved = conn.execute(
            text(
                """UPDATE agent_actions SET status = 'expired', outcome_code = 'expired'
                   WHERE id = :id AND status = 'awaiting_approval' RETURNING user_id, campaign_id"""
            ),
            {"id": found.agent_action_id},
        ).one_or_none()
        if moved is not None:
            _changed(conn, found.agent_action_id, moved.campaign_id, moved.user_id, "expired")
    return len(rows)


def cancel_open(conn: Connection, campaign_id: UUID, note: str) -> dict[str, int]:
    """A campaign is cancelled (CMP-07): its pending approvals expire with `note`, its awaiting and scheduled
    actions are
    cancelled and its waiting messages cancelled. A message already sending or sent keeps its action `scheduled` for the
    settle sweep. Runs on the connection the caller gives (the API's, for the candidate's own cancel)."""
    expired = conn.execute(
        text(
            """UPDATE approvals SET status = 'expired', decided_at = now(), note = :note
               WHERE status = 'pending' AND agent_action_id IN (SELECT id FROM agent_actions WHERE campaign_id = :c)
               RETURNING agent_action_id"""
        ),
        {"c": campaign_id, "note": note},
    ).all()
    open_rows = conn.execute(
        text(
            """SELECT id, user_id, status::text AS status FROM agent_actions
               WHERE campaign_id = :c AND kind = 'outside_action' AND status IN ('awaiting_approval', 'scheduled')
               ORDER BY id FOR UPDATE"""
        ),
        {"c": campaign_id},
    ).all()
    scheduled = [r.id for r in open_rows if r.status == "scheduled"]
    cancelled_messages = outbox.cancel_for_actions(conn, scheduled, "cancelled")
    states = outbox.action_states(conn, scheduled)
    cancelled = 0
    for found in open_rows:
        if states.get(found.id) in ("sending", "sent", "unknown"):
            continue
        _move(conn, found.id, "cancelled", outcome_code="cancelled")
        _changed(conn, found.id, campaign_id, found.user_id, "cancelled")
        cancelled += 1
    return {
        "approvals_expired": len(expired),
        "actions_cancelled": cancelled,
        "messages_cancelled": len(cancelled_messages),
    }


def _scheduled_follow_ups(conn: Connection, campaign_id: UUID) -> list[Any]:
    rows = conn.execute(
        text(
            """SELECT id, undo_until FROM agent_actions
               WHERE campaign_id = :c AND action_type = 'send_follow_up' AND status = 'scheduled' ORDER BY id"""
        ),
        {"c": campaign_id},
    ).all()
    return list(rows)


def hold(conn: Connection, campaign_id: UUID) -> int:
    """Keep the campaign's scheduled follow-ups from being sent while it is paused (decision 12). Returns how many."""
    return outbox.hold(conn, [r.id for r in _scheduled_follow_ups(conn, campaign_id)])


def unhold(conn: Connection, campaign_id: UUID) -> int:
    """Let the held follow-ups go again, each with at least a minute left so the candidate can still undo it."""
    now = datetime.now(timezone.utc)
    released = 0
    for found in _scheduled_follow_ups(conn, campaign_id):
        released += outbox.release(conn, [found.id], max(found.undo_until, now + HELD_MINIMUM))
    return released


def mark_executed(conn: Connection, action_id: int) -> bool:
    """A scheduled action has run (a follow-up was sent, or the candidate recorded that they applied): `executed`. False
    when it was not scheduled."""
    moved = conn.execute(
        text(
            """UPDATE agent_actions SET status = 'executed', executed_at = now()
               WHERE id = :id AND status = 'scheduled' RETURNING user_id, campaign_id"""
        ),
        {"id": action_id},
    ).one_or_none()
    if moved is None:
        return False
    _changed(conn, action_id, moved.campaign_id, moved.user_id, "executed")
    return True


def settle(conn: Connection) -> int:
    """The sweep: a follow-up whose message was sent is `executed`, one whose message failed is `failed` with the
    outbox's code, one whose message was cancelled by the platform is `cancelled`; `pending`, `sending` and `unknown`
    messages leave their action `scheduled`. Returns how many actions changed."""
    ids = [
        r.id
        for r in conn.execute(
            text("SELECT id FROM agent_actions WHERE action_type = 'send_follow_up' AND status = 'scheduled'")
        )
    ]
    states = outbox.action_states(conn, ids)
    changed = 0
    for action_id, state in states.items():
        if state not in ("sent", "failed", "cancelled"):
            continue
        try:
            with conn.begin_nested():
                if state == "sent":
                    done = mark_executed(conn, action_id)
                else:
                    code = conn.execute(
                        text("SELECT last_error_code FROM outbox WHERE agent_action_id = :id"), {"id": action_id}
                    ).scalar_one_or_none()
                    done = _finish_unsent(
                        conn, action_id, "failed" if state == "failed" else "cancelled", code or state
                    )
        except DBAPIError:
            log.event(
                "guardian.settle_refused", action_id=action_id
            )  # the trigger said no (for example the dial is now never)
            continue
        changed += int(done)
    return changed


def _finish_unsent(conn: Connection, action_id: int, status: str, code: str) -> bool:
    moved = conn.execute(
        text(
            """UPDATE agent_actions SET status = CAST(:s AS action_status), outcome_code = :code
               WHERE id = :id AND status = 'scheduled' RETURNING user_id, campaign_id"""
        ),
        {"id": action_id, "s": status, "code": code},
    ).one_or_none()
    if moved is None:
        return False
    _changed(conn, action_id, moved.campaign_id, moved.user_id, status)
    return True


# ---- what the pages read -------------------------------------------------------------------------------------------

_VIEW = """SELECT p.id AS approval_id, p.status::text AS status, p.requested_at, p.expires_at, p.decided_at, p.note,
                  a.id AS action_id, a.campaign_id, a.action_type::text AS type, a.status::text AS action_status,
                  a.summary, a.payload, a.undo_until, a.outcome_code,
                  COALESCE((SELECT jsonb_agg(jsonb_build_object('check', c.check_kind, 'passed', c.passed,
                                                                'reason_code', c.reason_code) ORDER BY c.check_kind)
                            FROM (SELECT DISTINCT ON (check_kind) check_kind, passed, reason_code FROM guardian_checks
                                  WHERE agent_action_id = a.id ORDER BY check_kind, id DESC) c), '[]'::jsonb) AS checks
           FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id"""


def _view(row: Any) -> dict[str, Any]:
    found = dict(row._mapping)
    found["payload_hash"] = payload_hash_of(found["payload"])
    found["editable_fields"] = EDITABLE_FIELDS.get(found["type"], [])
    return found


def approval_view(conn: Connection, user_id: UUID, approval_id: UUID) -> dict[str, Any] | None:
    """One approval with its action's payload, the hash the candidate approves, the editable fields and the latest
    verdict
    of each check. None for another person's approval."""
    row = conn.execute(
        text(_VIEW + " WHERE p.id = :id AND p.user_id = :u"), {"id": approval_id, "u": user_id}
    ).one_or_none()
    return None if row is None else _view(row)


def list_approvals(
    conn: Connection, user_id: UUID, *, status: str = "pending", limit: int = 50
) -> list[dict[str, Any]]:
    """The person's approvals of one status, the soonest to expire first for pending ones, else the newest decided."""
    order = "p.expires_at" if status == "pending" else "p.decided_at DESC"
    rows = conn.execute(
        text(_VIEW + f" WHERE p.user_id = :u AND p.status = CAST(:s AS approval_status) ORDER BY {order} LIMIT :n"),
        {"u": user_id, "s": status, "n": limit},
    ).all()
    return [_view(r) for r in rows]


def pending_counts(conn: Connection, user_id: UUID) -> dict[str, int]:
    """How many approvals wait for the person, in all and by action type."""
    counts = {
        r.type: r.n
        for r in conn.execute(
            text(
                """SELECT a.action_type::text AS type, count(*) AS n FROM approvals p
                   JOIN agent_actions a ON a.id = p.agent_action_id
                   WHERE p.user_id = :u AND p.status = 'pending' GROUP BY 1"""
            ),
            {"u": user_id},
        )
    }
    return {"total": sum(counts.values()), **counts}


# ---- the send-time recheck (the relay's resolver calls this) --------------------------------------------------------


def recheck(conn: Connection, action_id: int) -> tuple[str | None, dict[str, Any] | None]:
    """Run both keys again on a scheduled action just before its message is sent, with six new check rows. Returns
    (None, the action) when it may go, else (the reason code, the action or None): a failure blocks the action, and an
    action that is no longer scheduled is left as it is. The caller refuses the outbox row when a reason comes back."""
    row = conn.execute(
        text(
            """SELECT id, user_id, campaign_id, agent_run_id, action_type::text AS action_type, status::text AS status,
                      payload, inputs FROM agent_actions WHERE id = :id FOR UPDATE"""
        ),
        {"id": action_id},
    ).one_or_none()
    if row is None:
        return "action_not_scheduled", None
    found = dict(row._mapping)
    if row.status != "scheduled":
        return "action_not_scheduled", found
    gate = policy.read_gate(conn, "campaign", row.campaign_id) if row.campaign_id else None
    if gate is None:
        _block(conn, row, "campaign_closed")
        return "campaign_closed", found
    provenance = (row.inputs or {}).get("provenance") or {}
    result = _decide(conn, gate, row.action_type)
    results = _run_checks(conn, row.id, row.agent_run_id, gate, row.action_type, row.payload, provenance)
    reason = _first_reason(result, results)
    if reason is not None:
        _block(conn, row, reason)
        return reason, found
    return None, found
