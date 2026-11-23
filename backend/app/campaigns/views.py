"""What a campaign looks like to its candidate: the board, one campaign's detail, and the sentences both carry (07-06).

Everything here is a read of stored rows, and every sentence is a code template: a step's text, the next step, a pause,
a blocked follow-up. A reason comes from guardian.api.explain; the only words a model wrote that appear are the ones
that already passed validation and were stored (dossier claims, the prep questions, a follow-up's subject and body), and
each sits in its own field, never in a sentence of ours. Nothing here starts, approves or sends anything, and nothing
opens a worker or match connection: the caller's transaction (the API login, or a worker connection that filters by
`user_id` as every query below does) is the only one used.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.apply import api as apply
from app.guardian import api as guardian
from app.hiring import api as hiring
from app.platform import outbox
from app.platform.errors import ApiError

from .crew import research

STAGES = ("researching", "needs_you", "applied", "talking", "interview", "offer", "closed")
OPEN_STAGES = STAGES[:-1]
CLOSED_LIMIT = 50  # assumption: API 3.8, the closed column shows this many
PAUSE_REASONS = ("candidate", "budget_exhausted", "scam_suspected")
UNKNOWN_REASON = "This did not go ahead."  # for a code that has no template; never the code's own text
STEP_FAILED = "This step could not finish."

# ---- the sentences ----------------------------------------

NEXT_STEP = {
    "researching": "Building a cited company dossier.",
    "packet_review": "Your application packet is ready for review.",
    "follow_up_review": "A follow-up is waiting for your approval.",
    "follow_up_address": "Add the hiring contact's email to send a follow-up.",
    "follow_up_waiting": "Next follow-up on {date}",
    "budget_exhausted": "Paused at the budget. Raise it to continue.",
}
TODO_TEXT = {
    "add_follow_up_address": "Add the hiring contact's email to send a follow-up.",
    "no_supported_draft": "No follow-up could be written from what your record supports, so nothing will be sent.",
}
# (step key without its number, status class) -> sentence; a class is "active", "waiting", "done", "failed" or "other"
STEP_TEXT = {
    ("research", "active"): "Building a cited company dossier.",
    ("research", "done"): "Company dossier ready.",
    ("research", "failed"): "The company dossier could not be built.",
    ("fit", "active"): "Comparing the job's requirements with your record.",
    ("fit", "done"): "Fit with the job's requirements assessed.",
    ("packet_draft", "active"): "Drafting your application packet.",
    ("packet_draft", "done"): "Application packet drafted.",
    ("packet", "active"): "Preparing your application packet for your approval.",
    ("packet", "waiting"): "Your application packet is ready for review.",
    ("packet", "done"): "Application packet approved.",
    ("follow_up", "active"): "Drafting a follow-up.",
    ("follow_up", "waiting"): "Next follow-up on {date}",
    ("follow_up", "done"): "Follow-up handled.",
    ("interview_prep", "active"): "Preparing interview questions.",
    ("interview_prep", "done"): "Interview preparation ready.",
}
OTHER_STEP = {"skipped": "Skipped.", "cancelled": "Cancelled."}
DOSSIER_INCOMPLETE = " Some pages could not be read."

APPROVAL_TEXT = {  # (action type, approval status) -> sentence after "{company}: "
    ("send_follow_up", "pending"): "a follow-up is waiting for your approval.",
    ("send_follow_up", "approved"): "you approved a follow-up.",
    ("send_follow_up", "rejected"): "you declined a follow-up.",
    ("send_follow_up", "expired"): "a follow-up expired without an answer.",
    ("submit_application", "pending"): "your application packet is ready for review.",
    ("submit_application", "approved"): "you approved the application packet.",
    ("submit_application", "rejected"): "you declined the application packet.",
    ("submit_application", "expired"): "the application packet expired without an answer.",
}
ACTION_NOUN = {"send_follow_up": "a follow-up", "submit_application": "your application packet"}
ACTIVITY_TEXT = {  # action status -> sentence after "{company}: {noun} "
    "executed": "was {done}.",
    "blocked": "was blocked: {reason}",
    "failed": "failed: {reason}",
}
EXECUTED = {"send_follow_up": "sent", "submit_application": "recorded as applied"}


def reason_text(code: str | None, details: dict[str, Any] | None = None) -> str:
    """guardian.api.explain for a code, with the quote or detail a template needs taken from the check's details; a code
    without a template, or one whose parameter is missing, reads UNKNOWN_REASON."""
    if code is None:
        return UNKNOWN_REASON
    details = details or {}
    detail = details.get("what")
    params = {
        "quote": str(details.get("quote") or ""),
        "detail": ", ".join(detail) if isinstance(detail, list) else str(detail or ""),
    }
    for known in (params, {}):
        try:
            return guardian.explain(code, **known)
        except (KeyError, IndexError):
            continue
    return UNKNOWN_REASON


STEP_ORDER = ("research", "fit", "packet_draft", "packet", "follow_up", "interview_prep")


def step_base(step_key: str) -> str:
    return step_key.split(":", 1)[0]


def _step_position(step: Any) -> tuple[int, int]:
    """The order a campaign's steps run in, then a numbered step's number: steps made in one transaction share a
    creation time, so the pipeline's own order (not the clock) lists them."""
    base, _, number = step.step_key.partition(":")
    return (STEP_ORDER.index(base) if base in STEP_ORDER else len(STEP_ORDER), int(number) if number.isdigit() else 0)


def _class(status: str) -> str:
    if status in ("pending", "running"):
        return "active"
    return status if status in ("waiting", "done", "failed") else "other"


def _day(moment: datetime | None) -> str:
    return "" if moment is None else moment.astimezone(timezone.utc).date().isoformat()


def step_text(row: Any) -> tuple[str, str | None]:
    """(the step's sentence, its to-do sentence or None) from the step's key, status, due time and stored output."""
    output = row.output if isinstance(row.output, dict) else {}
    todo = TODO_TEXT.get(str(output.get("todo"))) if output.get("todo") else None
    base, status = step_base(row.step_key), str(row.status)
    if status in OTHER_STEP:
        return OTHER_STEP[status], todo
    kind = _class(status)
    if base == "follow_up" and kind == "active" and row.due_at is not None and row.due_at > datetime.now(timezone.utc):
        kind = "waiting"  # a timer that has not come due reads as the wait it is
    sentence = STEP_TEXT.get((base, kind))
    if sentence is None:
        sentence = STEP_FAILED if kind == "failed" else "Working on it." if kind == "active" else "Done."
    sentence = sentence.format(date=_day(row.due_at))
    if base == "research" and kind == "done" and output.get("incomplete"):
        sentence += DOSSIER_INCOMPLETE
    return sentence, todo


def usd(micro: int) -> str:
    """Micro-dollars as dollars with six decimals (a string, so no float is involved)."""
    return f"{Decimal(micro) / Decimal(1_000_000):.6f}"


# ---- reading rows ----------------------------------------

_CARD = """SELECT c.id, c.user_id, c.job_id, c.stage::text AS stage, c.paused_at,
                  c.closed_reason::text AS closed_reason,
                  c.closed_at, c.budget_microusd, c.follow_up_after_days, c.follow_up_email, c.introduction_id,
                  c.created_at, c.updated_at, j.title, j.company_name, j.apply_url,
                  GREATEST(c.updated_at, (SELECT max(a.created_at) FROM agent_actions a WHERE a.campaign_id = c.id))
                      AS last_activity_at
           FROM campaigns c JOIN jobs j ON j.id = c.job_id"""


@dataclass
class _Facts:
    """What the cards of one person need beyond the campaign row, read for all their campaigns at once."""

    steps: dict[UUID, list[Any]]
    approvals: dict[UUID, list[Any]]
    pauses: dict[UUID, Any]


def _facts(conn: Connection, user_id: UUID, ids: list[UUID]) -> _Facts:
    steps: dict[UUID, list[Any]] = {}
    approvals: dict[UUID, list[Any]] = {}
    pauses: dict[UUID, Any] = {}
    if not ids:
        return _Facts(steps, approvals, pauses)
    for row in conn.execute(
        text(
            """SELECT campaign_id, step_key, agent_role::text AS agent_role, status::text AS status, due_at, output,
                      error_code FROM campaign_steps WHERE user_id = :u AND campaign_id = ANY(CAST(:ids AS uuid[]))
               ORDER BY created_at, step_key"""
        ),
        {"u": user_id, "ids": ids},
    ):
        steps.setdefault(row.campaign_id, []).append(row)
    for rows in steps.values():
        rows.sort(key=_step_position)
    for row in conn.execute(
        text(
            """SELECT a.campaign_id, p.id, a.id AS action_id, a.action_type::text AS kind
               FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
               WHERE p.user_id = :u AND p.status = 'pending' AND a.campaign_id = ANY(CAST(:ids AS uuid[]))
               ORDER BY p.requested_at, p.id"""
        ),
        {"u": user_id, "ids": ids},
    ):
        approvals.setdefault(row.campaign_id, []).append(row)
    for row in conn.execute(  # the newest pause row of each campaign: its code is the reason, its inputs the quotes
        text(
            """SELECT DISTINCT ON (campaign_id) campaign_id, outcome_code, inputs FROM agent_actions
               WHERE user_id = :u AND kind = 'state_change' AND outcome_code = ANY(CAST(:reasons AS text[]))
                 AND campaign_id = ANY(CAST(:ids AS uuid[])) ORDER BY campaign_id, id DESC"""
        ),
        {"u": user_id, "reasons": list(PAUSE_REASONS), "ids": ids},
    ):
        pauses[row.campaign_id] = row
    return _Facts(steps, approvals, pauses)


def _pause(row: Any, pause: Any) -> tuple[str | None, list[dict[str, str]]]:
    """(the pause reason, the quoted scam signals) of a campaign row: None and [] when it is running."""
    if row.paused_at is None:
        return None, []
    reason = pause.outcome_code if pause is not None else "candidate"
    inputs = pause.inputs if pause is not None and isinstance(pause.inputs, dict) else {}
    signals = [
        {"kind": str(s.get("kind", "")), "quote": str(s.get("quote", ""))}
        for s in inputs.get("signals", [])
        if isinstance(s, dict)
    ]
    return reason, signals


def pause_sentence(reason: str, signals: list[dict[str, str]]) -> str:
    return reason_text(reason, {"quote": signals[0]["quote"] if signals else ""})


def _active_step(steps: list[Any]) -> Any | None:
    return next((s for s in steps if s.status in ("running", "waiting", "pending")), None)


def _next_step(
    row: Any, steps: list[Any], approvals: list[Any], reason: str | None, signals: list[dict[str, str]]
) -> Any:
    """The card's next_step: {step_key, agent_role, text}, or None when there is nothing to say."""
    if row.stage == "closed":
        return None
    active = _active_step(steps)
    if reason is not None:
        text_ = NEXT_STEP["budget_exhausted"] if reason == "budget_exhausted" else pause_sentence(reason, signals)
        return {
            "step_key": active.step_key if active else "campaign",
            "agent_role": active.agent_role if active else "guardian",
            "text": text_,
        }
    by_action = {}
    for step in steps:
        action = step.output.get("action_id") if isinstance(step.output, dict) else None
        if action is not None:
            by_action[int(action)] = step
    for approval in approvals:  # a packet before a follow-up: the stages run in that order
        if approval.kind == "submit_application":
            return {"step_key": "packet", "agent_role": "operator", "text": NEXT_STEP["packet_review"]}
    for approval in approvals:
        step = by_action.get(approval.action_id)
        return {
            "step_key": step.step_key if step else "follow_up",
            "agent_role": step.agent_role if step else "chaser",
            "text": NEXT_STEP["follow_up_review"],
        }
    for step in steps:
        if step_base(step.step_key) == "research" and step.status in ("pending", "running"):
            return {"step_key": step.step_key, "agent_role": step.agent_role, "text": NEXT_STEP["researching"]}
    for step in steps:
        if step_base(step.step_key) != "follow_up":
            continue
        output = step.output if isinstance(step.output, dict) else {}
        if output.get("todo") == "add_follow_up_address":
            return {"step_key": step.step_key, "agent_role": step.agent_role, "text": NEXT_STEP["follow_up_address"]}
        if step.status in ("pending", "waiting") and "action_id" not in output and "todo" not in output:
            return {
                "step_key": step.step_key,
                "agent_role": step.agent_role,
                "text": NEXT_STEP["follow_up_waiting"].format(date=_day(step.due_at)),
            }
    return None


def _card(row: Any, facts: _Facts) -> dict[str, Any]:
    steps, approvals = facts.steps.get(row.id, []), facts.approvals.get(row.id, [])
    reason, signals = _pause(row, facts.pauses.get(row.id))
    return {
        "id": row.id,
        "title": row.title,
        "company_name": row.company_name,
        "from_introduction": row.introduction_id is not None,
        "paused_at": row.paused_at,
        "pause_reason": reason,
        "closed_reason": row.closed_reason,
        "next_step": _next_step(row, steps, approvals, reason, signals),
        "pending_approvals": len(approvals),
        "last_activity_at": row.last_activity_at,
    }


def cards(
    conn: Connection,
    user_id: UUID,
    where: str,
    params: dict[str, Any],
    *,
    order: str = "c.updated_at DESC",
    limit: int = 1000,
) -> list[tuple[Any, dict[str, Any]]]:
    """(campaign row, card) for the person's campaigns that match `where`; `where` is a fixed string of ours."""
    sql = f"{_CARD} WHERE c.user_id = :u AND {where} ORDER BY {order} LIMIT :limit"
    rows = conn.execute(text(sql), {"u": user_id, "limit": limit, **params}).all()
    facts = _facts(conn, user_id, [r.id for r in rows])
    return [(r, _card(r, facts)) for r in rows]


def board(conn: Connection, user_id: UUID, *, include_closed: bool = False) -> dict[str, Any]:
    """API 3.8: one column per stage in order, six without the closed one. The closed column holds the CLOSED_LIMIT
    most recently closed and the total."""
    columns: list[dict[str, Any]] = [{"stage": s, "campaigns": []} for s in OPEN_STAGES]
    placed = {c["stage"]: c for c in columns}
    for row, card in cards(conn, user_id, "c.stage <> 'closed'", {}):
        placed[row.stage]["campaigns"].append(card)
    if include_closed:
        total = conn.execute(
            text("SELECT count(*) FROM campaigns WHERE user_id = :u AND stage = 'closed'"), {"u": user_id}
        ).scalar_one()
        shown = cards(conn, user_id, "c.stage = 'closed'", {}, order="c.closed_at DESC, c.id DESC", limit=CLOSED_LIMIT)
        columns.append({"stage": "closed", "campaigns": [card for _, card in shown], "total": int(total)})
    return {"columns": columns}


def _action_rows(conn: Connection, user_id: UUID, campaign_id: UUID) -> list[Any]:
    sql = """SELECT a.id, a.status::text AS status, a.payload, a.undo_until, a.outcome_code, a.inputs,
                    p.id AS approval_id, p.status::text AS approval_status, a.action_type::text AS action_type
             FROM agent_actions a LEFT JOIN approvals p ON p.agent_action_id = a.id
             WHERE a.user_id = :u AND a.campaign_id = :c AND a.kind = 'outside_action' ORDER BY a.id"""
    return list(conn.execute(text(sql), {"u": user_id, "c": campaign_id}).all())


def _failed_details(conn: Connection, action_id: int) -> dict[str, Any]:
    """The details of the newest failed check of an action, for the quote or detail of its reason."""
    found = conn.execute(
        text(
            """SELECT details FROM guardian_checks WHERE agent_action_id = :a AND NOT passed
               ORDER BY id DESC LIMIT 1"""
        ),
        {"a": action_id},
    ).scalar()
    return found if isinstance(found, dict) else {}


def _follow_ups(conn: Connection, actions: list[Any], steps: list[Any]) -> list[dict[str, Any]]:
    mine = [a for a in actions if a.action_type == "send_follow_up" and a.outcome_code != "edited"]
    step_of = {
        int(s.output["action_id"]): s.step_key
        for s in steps
        if isinstance(s.output, dict) and s.output.get("action_id") is not None
    }
    states = outbox.action_states(conn, [a.id for a in mine])
    found = []
    for a in mine:
        replaced = a.inputs.get("replaces") if isinstance(a.inputs, dict) else None
        payload = a.payload if isinstance(a.payload, dict) else {}
        found.append(
            {
                "step_key": step_of.get(a.id) or (step_of.get(int(replaced)) if replaced is not None else None),
                "action_id": a.id,
                "approval_id": a.approval_id,
                "status": a.status,
                "delivery": states.get(a.id),
                "to": payload.get("to"),
                "subject": payload.get("subject"),
                "body": payload.get("body"),
                "undo_until": a.undo_until,
                "outcome_code": a.outcome_code,
                "outcome_text": reason_text(a.outcome_code, _failed_details(conn, a.id)) if a.outcome_code else None,
            }
        )
    return found


def _packet(conn: Connection, user_id: UUID, steps: list[Any], actions: list[Any]) -> dict[str, Any] | None:
    by_key = {s.step_key: s.output for s in steps if isinstance(s.output, dict)}
    packet_id = by_key.get("packet_draft", {}).get("packet_id")
    if packet_id is None:
        return None
    try:
        view = apply.get_packet(conn, user_id, UUID(str(packet_id)))
    except ApiError:  # a packet that is gone reads as no packet
        return None
    shape = by_key.get("packet", {})
    action_id = shape.get("action_id")
    action = next((a for a in actions if a.id == action_id), None)
    approval_id = shape.get("approval_id")
    return {
        "id": view.id,
        "status": view.status,
        "approval_id": UUID(str(approval_id)) if approval_id else None,
        "action_status": None if action is None else action.status,
    }


def _missing_topics(claims: list[Any]) -> list[str]:
    """The topics no claim covers; a dossier whose claims name no topic reports none."""
    topical = [c for c in claims if isinstance(c, dict) and c.get("topic")]
    return [] if claims and not topical else research.missing_topics(topical)


def _dossier(conn: Connection, user_id: UUID, campaign_id: UUID, steps: list[Any]) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            """SELECT id, version, claims, company_name, created_at FROM company_dossiers
               WHERE user_id = :u AND campaign_id = :c ORDER BY version DESC LIMIT 1"""
        ),
        {"u": user_id, "c": campaign_id},
    ).one_or_none()
    if row is None:
        return None
    done = next((s.output for s in steps if s.step_key == "research" and isinstance(s.output, dict)), {})
    claims = row.claims if isinstance(row.claims, list) else []
    return {
        "id": row.id,
        "version": row.version,
        "company_name": row.company_name,
        "created_at": row.created_at,
        "incomplete": bool(done.get("incomplete", False)),
        "claims": claims,
        "missing_topics": _missing_topics(claims),
    }


def detail(conn: Connection, user_id: UUID, campaign_id: UUID) -> dict[str, Any] | None:
    """One campaign of this person in the INDEX shape, or None when it is not theirs."""
    found = cards(conn, user_id, "c.id = :id", {"id": campaign_id})
    if not found:
        return None
    row, card = found[0]
    facts = _facts(conn, user_id, [row.id])
    steps = facts.steps.get(row.id, [])
    actions = _action_rows(conn, user_id, row.id)
    reason, signals = _pause(row, facts.pauses.get(row.id))
    outputs = {step_base(s.step_key): s.output for s in steps if isinstance(s.output, dict)}
    introduction = None
    if row.introduction_id is not None:
        ref = hiring.introduction_ref(conn, row.introduction_id)
        if ref is not None:
            introduction = {
                "id": row.introduction_id,
                "employer_answer": ref.employer_answer,
                "answer_due_at": ref.answer_due_at,
            }
    rendered = []
    for step in steps:
        sentence, todo = step_text(step)
        rendered.append(
            {
                "step_key": step.step_key,
                "agent_role": step.agent_role,
                "status": step.status,
                "due_at": step.due_at,
                "error_code": step.error_code,
                "todo": todo,
                "text": sentence,
            }
        )
    return {
        "id": row.id,
        "job": {"id": row.job_id, "title": row.title, "company_name": row.company_name, "apply_url": row.apply_url},
        "stage": row.stage,
        "from_introduction": row.introduction_id is not None,
        "introduction": introduction,
        "paused_at": row.paused_at,
        "pause_reason": reason,
        "pause_text": None if reason is None else pause_sentence(reason, signals),
        "scam_signals": signals,
        "closed_at": row.closed_at,
        "closed_reason": row.closed_reason,
        "budget_usd": usd(row.budget_microusd),
        "spent_usd": usd(guardian.spent_microusd(conn, campaign_id=row.id)),
        "follow_up_after_days": row.follow_up_after_days,
        "follow_up_email": row.follow_up_email,
        "created_at": row.created_at,
        "server_time": datetime.now(timezone.utc),
        "next_step": card["next_step"],
        "steps": rendered,
        "dossier": _dossier(conn, user_id, row.id, steps),
        "fit": outputs.get("fit"),
        "packet": _packet(conn, user_id, steps, actions),
        "prep": outputs.get("interview_prep"),
        "follow_ups": _follow_ups(conn, actions, steps),
        "pending_approvals": [{"id": a.id, "kind": a.kind} for a in facts.approvals.get(row.id, [])],
    }


# ---- by job (chat) ----------------------------------------


def for_job(conn: Connection, user_id: UUID, job_id: int) -> dict[str, Any] | None:
    """The person's open campaign for the job, else the latest: the card plus `stage`, `job_id` and step texts."""
    found = cards(conn, user_id, "c.job_id = :j", {"j": job_id}, order="(c.stage = 'closed'), c.created_at DESC")
    if not found:
        return None
    row, card = found[0]
    steps = _facts(conn, user_id, [row.id]).steps.get(row.id, [])
    return {
        **card,
        "stage": row.stage,
        "job_id": row.job_id,
        "steps": [{"step_key": s.step_key, "status": s.status, "text": step_text(s)[0]} for s in steps],
    }


def open_campaign_for_job(conn: Connection, user_id: UUID, job_id: int) -> UUID | None:
    found: UUID | None = conn.execute(
        text("SELECT id FROM campaigns WHERE user_id = :u AND job_id = :j AND stage <> 'closed'"),
        {"u": user_id, "j": job_id},
    ).scalar()
    return found


def open_campaigns(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """Every open campaign of the person as for_job's shape, newest first (what chat lists without a job)."""
    out = []
    for row, card in cards(conn, user_id, "c.stage <> 'closed'", {}, order="c.created_at DESC"):
        out.append({**card, "stage": row.stage, "job_id": row.job_id})
    return out


def dossier_for_job(conn: Connection, user_id: UUID, job_id: int) -> dict[str, Any] | None:
    """The newest dossier of the person's latest campaign for the job that has one, with its claims' sources."""
    campaign = conn.execute(
        text(
            """SELECT c.id FROM campaigns c WHERE c.user_id = :u AND c.job_id = :j
                 AND EXISTS (SELECT 1 FROM company_dossiers d WHERE d.campaign_id = c.id)
               ORDER BY (c.stage = 'closed'), c.created_at DESC LIMIT 1"""
        ),
        {"u": user_id, "j": job_id},
    ).scalar()
    if campaign is None:
        return None
    steps = _facts(conn, user_id, [campaign]).steps.get(campaign, [])
    return {"campaign_id": campaign, **(_dossier(conn, user_id, campaign, steps) or {})}


def blocks_for_job(conn: Connection, user_id: UUID, job_id: int) -> list[dict[str, str]]:
    """Why work stopped or an action did not go ahead on the job's campaign: the pause now in force and every blocked,
    failed, expired or cancelled action, each as {kind, what, text} with a sentence from guardian.api.explain."""
    found = cards(conn, user_id, "c.job_id = :j", {"j": job_id}, order="(c.stage = 'closed'), c.created_at DESC")
    if not found:
        return []
    row, card = found[0]
    out: list[dict[str, str]] = []
    if card["pause_reason"] is not None:
        signals = _pause(row, _facts(conn, user_id, [row.id]).pauses.get(row.id))[1]
        out.append(
            {"kind": "pause", "what": card["pause_reason"], "text": pause_sentence(card["pause_reason"], signals)}
        )
    for a in _action_rows(conn, user_id, row.id):
        if a.status in ("blocked", "failed", "expired", "cancelled", "rejected") and a.outcome_code != "edited":
            out.append(
                {
                    "kind": "action",
                    "what": ACTION_NOUN.get(a.action_type, "an action"),
                    "text": reason_text(a.outcome_code, _failed_details(conn, a.id)),
                }
            )
    return out


# ---- the brief ----------------------------------------


@dataclass(frozen=True)
class BriefLine:
    """One approval or action as the brief says it: where it leads and the sentence."""

    campaign_id: UUID
    company_name: str
    status: str
    text: str
    link: str


def link(campaign_id: UUID) -> str:
    return f"/app/campaigns/{campaign_id}"


def approval_lines(conn: Connection, user_id: UUID, approval_ids: list[UUID]) -> dict[UUID, BriefLine]:
    """Each approval of the person as its current state reads now (decided since it was listed: not pending)."""
    lines = {}
    for r in conn.execute(
        text(
            """SELECT p.id, p.status::text AS status, a.action_type::text AS kind, a.campaign_id, j.company_name
               FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
               JOIN campaigns c ON c.id = a.campaign_id JOIN jobs j ON j.id = c.job_id
               WHERE p.user_id = :u AND p.id = ANY(CAST(:ids AS uuid[]))"""
        ),
        {"u": user_id, "ids": approval_ids},
    ):
        sentence = APPROVAL_TEXT.get((r.kind, r.status))
        if sentence is not None:
            lines[r.id] = BriefLine(
                r.campaign_id, r.company_name, r.status, f"{r.company_name}: {sentence}", link(r.campaign_id)
            )
    return lines


def action_lines(conn: Connection, user_id: UUID, action_ids: list[int]) -> dict[int, BriefLine]:
    """Each action as its state reads now: executed, blocked or failed with its reason; any other state says so."""
    lines = {}
    for r in conn.execute(
        text(
            """SELECT a.id, a.status::text AS status, a.action_type::text AS kind, a.outcome_code, a.campaign_id,
                      j.company_name FROM agent_actions a JOIN campaigns c ON c.id = a.campaign_id
               JOIN jobs j ON j.id = c.job_id WHERE a.user_id = :u AND a.id = ANY(CAST(:ids AS bigint[]))"""
        ),
        {"u": user_id, "ids": action_ids},
    ):
        noun = ACTION_NOUN.get(r.kind, "an action")
        if r.status in ACTIVITY_TEXT:
            reason = reason_text(r.outcome_code, _failed_details(conn, r.id)) if r.status != "executed" else ""
            sentence = ACTIVITY_TEXT[r.status].format(done=EXECUTED.get(r.kind, "done"), reason=reason)
        else:
            sentence = f"is now {r.status.replace('_', ' ')}."
        lines[r.id] = BriefLine(
            r.campaign_id, r.company_name, r.status, f"{r.company_name}: {noun} {sentence}", link(r.campaign_id)
        )
    return lines


def undoable(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """Scheduled follow-ups whose undo window is still open, soonest first (Today's receipt line)."""
    rows = conn.execute(
        text(
            """SELECT a.id, a.campaign_id, j.company_name, a.undo_until
               FROM agent_actions a JOIN campaigns c ON c.id = a.campaign_id JOIN jobs j ON j.id = c.job_id
               WHERE a.user_id = :u AND a.action_type = 'send_follow_up' AND a.status = 'scheduled'
                 AND a.undo_until > now() ORDER BY a.undo_until"""
        ),
        {"u": user_id},
    )
    return [
        {"action_id": r.id, "campaign_id": r.campaign_id, "company_name": r.company_name, "undo_until": r.undo_until}
        for r in rows
    ]


BRIEF_LIMIT = 5  # assumption: INDEX Brief items, each kind


def pending_approval_ids(conn: Connection, user_id: UUID) -> list[UUID]:
    return list(
        conn.execute(
            text(
                """SELECT p.id FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
                   WHERE p.user_id = :u AND p.status = 'pending' AND a.campaign_id IS NOT NULL
                   ORDER BY p.expires_at, p.id LIMIT :n"""
            ),
            {"u": user_id, "n": BRIEF_LIMIT},
        ).scalars()
    )


def activity_action_ids(conn: Connection, user_id: UUID, since: datetime | None) -> list[int]:
    """Outside actions executed, blocked or failed after `since` (a blocked one by its newest check), newest first."""
    if since is None:
        return []
    return list(
        conn.execute(
            text(
                """SELECT a.id FROM agent_actions a
                   WHERE a.user_id = :u AND a.kind = 'outside_action' AND a.campaign_id IS NOT NULL
                     AND a.status IN ('executed', 'blocked', 'failed')
                     AND COALESCE(a.executed_at,
                                  GREATEST(a.created_at, (SELECT max(g.checked_at) FROM guardian_checks g
                                                          WHERE g.agent_action_id = a.id))) > :since
                   ORDER BY a.id DESC LIMIT :n"""
            ),
            {"u": user_id, "since": since, "n": BRIEF_LIMIT},
        ).scalars()
    )
