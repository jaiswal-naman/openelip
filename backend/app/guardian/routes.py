"""The Guardian's HTTP routes (API.md 2.12, 2.13, 2.19, 3.6, 3.9, 3.16; CMP-03, CMP-04, GRD-01 to GRD-04):

    GET  /v1/approvals                          the person's approvals (pending by default), filtered and paginated
    GET  /v1/approvals/{id}                     one, with the exact payload, its hash and the checks already run
    POST /v1/approvals/{id}/approve             the yes, against the hash the person saw; optional edits
    POST /v1/approvals/{id}/reject              the no; nothing leaves
    GET  /v1/autonomy, PUT /v1/autonomy         the dial per action type, the undo window and the version (ETag)
    GET  /v1/control/recorder                   one campaign's (or run's) steps in order
    GET  /v1/control/recorder/{entry_id}        one step in full
    POST /v1/control/actions/{action_id}/undo   take back a follow-up still in its undo window

Every handler calls `guardian.api` and runs on the API login. Approve only records the yes and queues `guardian.release`
(the worker reruns both keys and schedules or blocks): nothing here can write an outside action or a check, and nothing
here opens a worker or match transaction. Recorder content is the person's own, so another person's id is a 404.
"""

import base64
import json
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from app.platform import db
from app.platform.api import envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import api as guardian

router = APIRouter()

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]

DEFAULT_LIMIT = 50  # assumption: the platform has no shared page size; the introductions list uses the same pair
MAX_LIMIT = 200
SCAN_LIMIT = 1000  # assumption: most approvals one list read looks through before filtering by campaign and kind
MAX_NOTE = 500  # assumption: characters in a rejection note
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such resource.")


def _usd(microusd: int | None, places: int = 6) -> str:
    return f"{Decimal(microusd or 0).scaleb(-6):.{places}f}"


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _offset(cursor: str | None) -> int:
    """The position a cursor names; a cursor that is not one of ours is 400."""
    if cursor is None:
        return 0
    try:
        found = json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"]
        if isinstance(found, int) and found >= 0:
            return found
    except (ValueError, KeyError, TypeError):
        pass
    raise ApiError(400, "invalid_request", "The cursor is not valid.")


def _page(items: list[Any], offset: int, limit: int) -> tuple[list[Any], str | None]:
    page = items[offset : offset + limit]
    return page, _cursor(offset + limit) if offset + limit < len(items) else None


# ---- approvals ------------------------------------------------------------------------------------------------------


def _approval(view: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """API 3.6's approval. The action's role and policy decision come from its recorder entry (the detail read)."""
    action = {"id": view["action_id"], "status": view["action_status"]}
    if detail is not None:
        action = {
            "id": view["action_id"],
            "agent_role": detail["agent_role"],
            "status": view["action_status"],
            "policy_decision": detail["policy_decision"],
        }
    return {
        "id": view["approval_id"],
        "kind": view["type"],
        "status": view["status"],
        "campaign_id": view["campaign_id"],
        "action": action,
        "summary": view["summary"],
        "payload": view["payload"],
        "payload_hash": view["payload_hash"],
        "editable_fields": view["editable_fields"],
        "needs_reply": False,  # a Twin question needing the candidate's reply arrives in Phase 9
        "checks": view["checks"],
        "undo_until": view["undo_until"],
        "outcome_code": view["outcome_code"],
        "requested_at": view["requested_at"],
        "decided_at": view["decided_at"],
        "note": view["note"],
        "expires_at": view["expires_at"],
    }


@router.get("/v1/approvals")
def approvals(
    principal: Signed,
    status: Annotated[Literal["pending", "approved", "rejected", "expired"], Query()] = "pending",
    campaign_id: Annotated[UUID | None, Query()] = None,
    kind: Annotated[Literal["submit_application", "send_follow_up", "twin_answer"] | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """The person's approvals of one status, the soonest to expire first (pending) or the newest decided."""
    offset = _offset(cursor)
    with db.transaction(principal.user_id) as conn:
        found = guardian.list_approvals(conn, principal.user_id, status=status, limit=SCAN_LIMIT)
    kept = [v for v in found if (campaign_id is None or v["campaign_id"] == campaign_id) and kind in (None, v["type"])]
    page, next_cursor = _page(kept, offset, limit)
    return envelope([_approval(v) for v in page], next_cursor=next_cursor, limit=limit)


@router.get("/v1/approvals/{approval_id}")
def read_approval(approval_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        view = guardian.approval_view(conn, principal.user_id, approval_id)
        if view is None:
            raise _not_found()
        detail = guardian.entry(conn, principal.user_id, view["action_id"])
    return envelope(_approval(view, detail))


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_hash: str = Field(min_length=1, max_length=128)
    edits: dict[str, Any] | None = None
    reply: Any = None  # a Twin answer's reply: Phase 9; refused here


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=MAX_NOTE)


def _decision(decision: guardian.Decision, kind: str) -> dict[str, Any]:
    return {
        "approval": {
            "id": decision.approval_id,
            "status": decision.approval_status,
            "note": decision.note,
            "decided_at": decision.decided_at,
        },
        "action": {"id": decision.action_id, "type": kind, "status": decision.action_status},
    }


@router.post("/v1/approvals/{approval_id}/approve")
def approve(approval_id: UUID, body: ApproveIn, write: Idempotent) -> Response:
    """Say yes to exactly the payload the person saw. The answer is recorded now and `release` is `pending`: the worker
    runs both keys again and schedules or blocks the action, and the page polls until it is `scheduled` or `blocked`.
    With `edits` this approval is `rejected` (note `edited`) and the replacement action follows the same way."""
    if body.reply is not None:
        raise ApiError(
            422,
            "validation_failed",
            "Replies to employer questions arrive with the Twin; this approval takes none.",
            {"fields": [{"field": "reply", "issue": "not_supported"}]},
        )

    def effect(conn: Connection) -> dict[str, Any]:
        view = guardian.approval_view(conn, write.user_id, approval_id)
        if view is None:
            raise _not_found()
        decision = guardian.decide(
            conn, write.user_id, approval_id, approve=True, payload_hash=body.payload_hash, edits=body.edits
        )
        return {**_decision(decision, view["type"]), "release": decision.release}

    return write.run(effect)


@router.post("/v1/approvals/{approval_id}/reject")
def reject(approval_id: UUID, write: Idempotent, body: RejectIn | None = None) -> Response:
    """Say no, with an optional note. Nothing leaves."""
    note = body.note if body else None

    def effect(conn: Connection) -> dict[str, Any]:
        view = guardian.approval_view(conn, write.user_id, approval_id)
        if view is None:
            raise _not_found()
        decision = guardian.decide(
            conn, write.user_id, approval_id, approve=False, payload_hash=view["payload_hash"], note=note
        )
        return _decision(decision, view["type"])

    return write.run(effect)


# ---- autonomy -------------------------------------------------------------------------------------------------------


def _autonomy(view: guardian.AutonomyView) -> dict[str, Any]:
    return {
        "version": view.version,
        "actions": {row.action_type: {"level": row.level, "allowed_levels": list(row.allowed)} for row in view.rows},
        "profile_sharing": view.profile_sharing,
        "undo_window_minutes": view.undo_window_minutes,
        "campaign_budget_default_usd": _usd(view.default_budget_microusd, 2),
    }


def _etag(version: str) -> str:
    return f'"{version}"'


@router.get("/v1/autonomy")
def read_autonomy(principal: Signed) -> Response:
    with db.transaction(principal.user_id) as conn:
        data = _autonomy(guardian.get_autonomy(conn, principal.user_id))
    return JSONResponse(envelope(data), headers={"ETag": _etag(data["version"])})


class LevelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = Field(min_length=1, max_length=32)


class AutonomyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: dict[str, LevelIn] | None = None
    undo_window_minutes: int | None = None


@router.put("/v1/autonomy")
def put_autonomy(body: AutonomyIn, write: Idempotent, if_match: Annotated[str | None, Header()] = None) -> Response:
    """Replace the given levels and the undo window. A level the type does not allow refuses the whole body (422
    `autonomy_not_allowed`) and saves nothing; a stale `If-Match` is 412 `version_conflict`."""
    version = None if if_match is None else if_match.strip().removeprefix("W/").strip('"')

    def effect(conn: Connection) -> dict[str, Any]:
        levels = {kind: given.level for kind, given in (body.actions or {}).items()}
        try:
            view = guardian.put_autonomy(
                conn,
                write.user_id,
                levels=levels,
                undo_window_minutes=body.undo_window_minutes,
                version=version,
            )
        except ApiError as refused:
            if refused.code == "autonomy_not_allowed":
                details = refused.details
                raise ApiError(
                    422,
                    "autonomy_not_allowed",
                    refused.message,
                    {
                        "action_type": details.get("action_type"),
                        "requested_level": details.get("level"),
                        "allowed_levels": details.get("allowed"),
                    },
                ) from refused
            raise
        return _autonomy(view)

    response = write.run(effect)
    stored = json.loads(bytes(response.body)).get("data") or {}
    if "version" in stored:
        response.headers["ETag"] = _etag(stored["version"])
    return response


# ---- the flight recorder and undo -----------------------------------------------------------------------------------


def _own(conn: Connection, table: Literal["campaigns", "agent_runs"], row_id: UUID) -> None:
    """404 unless row-level security shows the person this campaign or run."""
    if conn.execute(text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": row_id}).first() is None:
        raise _not_found()


@router.get("/v1/control/recorder")
def recorder(
    principal: Signed,
    campaign_id: Annotated[UUID | None, Query()] = None,
    run_id: Annotated[UUID | None, Query()] = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Every entry of a campaign or a run in order (exactly one of the two ids). Each entry is one recorded step; the
    Guardian's checks, the approval and the outcome belong to its row."""
    if (campaign_id is None) == (run_id is None):
        raise ApiError(
            422,
            "validation_failed",
            "Give exactly one of campaign_id and run_id.",
            {"fields": [{"field": "campaign_id", "issue": "exactly_one_of_campaign_id_run_id"}]},
        )
    offset = _offset(cursor)
    with db.transaction(principal.user_id) as conn:
        if campaign_id is not None:
            _own(conn, "campaigns", campaign_id)
        elif run_id is not None:
            _own(conn, "agent_runs", run_id)
        entries = guardian.timeline(conn, principal.user_id, campaign_id=campaign_id, run_id=run_id)
    page, next_cursor = _page(entries, offset, limit)
    data = [
        {
            "id": e["id"],
            "run_id": e["run_id"],
            "seq": e["seq"],
            "created_at": e["created_at"],
            "agent_role": e["agent_role"],
            "kind": e["kind"],
            "action_type": e["action_type"],
            "status": e["status"],
            "summary": e["summary"],
            "outcome_code": e["outcome_code"],
            "cost_usd": _usd(e["cost_microusd"]),
            "evidence_count": e["evidence_count"],
            "checks": e["guardian"] or [],
            "approval": e["approval"],
            "decided_at": e["decided_at"],
            "executed_at": e["executed_at"],
        }
        for e in page
    ]
    return envelope(data, next_cursor=next_cursor, limit=limit)


@router.get("/v1/control/recorder/{entry_id}")
def recorder_entry(entry_id: int, principal: Signed) -> dict[str, Any]:
    """One entry in full: inputs, evidence, model calls, the policy decision, every check with its reason and the
    payload. Reading it calls no model."""
    if not 1 <= entry_id <= 2**63 - 1:
        raise _not_found()
    with db.transaction(principal.user_id) as conn:
        found = guardian.entry(conn, principal.user_id, entry_id)
    if found is None:
        raise _not_found()
    cost = found.pop("cost_microusd")
    return envelope({**found, "cost_usd": _usd(cost)})


@router.post("/v1/control/actions/{action_id}/undo")
def undo(action_id: int, write: Idempotent) -> Response:
    """Take back a follow-up still waiting in its undo window. After the relay claimed the message it is `409 conflict`
    with the state (`sending`, `sent`, `executed`)."""
    if not 1 <= action_id <= 2**63 - 1:
        raise _not_found()

    def effect(conn: Connection) -> dict[str, Any]:
        done = guardian.undo(conn, write.user_id, action_id)
        return {"action": {"id": done.action_id, "status": done.status}, "outbox_cancelled": done.outbox_cancelled}

    return write.run(effect)
