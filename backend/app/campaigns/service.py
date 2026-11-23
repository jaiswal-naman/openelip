"""What the candidate does to a campaign (API.md 2.11, 3.7): start, change, pause, resume and cancel, and the start that
an accepted introduction causes (INDEX decision 8).

Every function takes a connection that acts for the campaign's owner. The routes pass the API login's connection; only
`from_introduction` runs on the worker's (the `introduction.accepted` subscriber). Nothing here opens a transaction of
its own and nothing calls a model. Stage, pause and closing changes go through `stages` (INDEX decision 7); the
Guardian's side of a cancel (pending approvals, scheduled messages) through `guardian.api.cancel_open`.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.apply import api as apply
from app.guardian import api as guardian
from app.hiring import api as hiring
from app.ingest import api as ingest
from app.match import api as match
from app.platform import config
from app.platform.errors import ApiError
from app.trust import api as trust

from . import gate, stages

MAX_BUDGET_USD = Decimal(100)  # assumption: the largest model budget one campaign may be given
CANCEL_ATTEMPTS = 3  # assumption: tries when another writer moved the campaign between the read and the write
OFFER_REASONS = ("offer_accepted", "offer_declined")
DEFAULT_CANCEL_NOTE = "You cancelled this campaign."

# What the first look at a campaign says about its next step, by step key (the board's views are 07-06's).
_NEXT_TEXT = {
    "research": ("researcher", "Building a cited company dossier."),
    "fit": ("analyst", "Checking the requirements against your evidence."),
    "packet_draft": ("writer", "Drafting your application packet."),
    "packet": ("operator", "Your application packet is ready for review."),
    "follow_up": ("chaser", "Waiting to draft a follow-up."),
    "interview_prep": ("coach", "Preparing your interview questions."),
}

_ROW = text(
    """SELECT c.id, c.user_id, c.job_id, c.stage::text AS stage, c.paused_at, c.closed_reason::text AS closed_reason,
              c.closed_at, c.budget_microusd, c.follow_up_after_days, c.follow_up_email, c.created_at,
              c.introduction_id, c.lock_version, j.title, j.company_name
       FROM campaigns c JOIN jobs j ON j.id = c.job_id WHERE c.id = :id"""
)


def usd_text(microusd: int, *, places: int = 2) -> str:
    """Micro-dollars as dollars: at least `places` decimals, more only when the amount needs them (1.5 -> "1.50")."""
    amount = Decimal(microusd).scaleb(-6)
    shown = format(amount.normalize(), "f")
    whole, _, fraction = shown.partition(".")
    return f"{whole}.{fraction.ljust(places, '0')}"


def budget_microusd(value: Decimal) -> int:
    """The validated budget in micro-dollars: more than zero, at most MAX_BUDGET_USD, at most 6 decimals."""
    exponent = value.as_tuple().exponent
    if not value.is_finite() or value <= 0 or value > MAX_BUDGET_USD or not isinstance(exponent, int) or exponent < -6:
        raise ApiError(
            422,
            "validation_failed",
            "The budget must be more than 0 and at most 100 US dollars, with up to 6 decimals.",
            {"fields": [{"field": "budget_usd", "issue": "out_of_range"}]},
        )
    return int(value * 1_000_000)


def _row(conn: Connection, campaign_id: UUID) -> Any:
    """The campaign as the connection's person sees it; 404 for another person's (row-level security hides it)."""
    row = conn.execute(_ROW, {"id": campaign_id}).one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "No such resource.")
    return row


def _closed() -> ApiError:
    return ApiError(409, "conflict", "This campaign is closed.", {"state": "closed"})


def _pause_reason(conn: Connection, campaign_id: UUID) -> str | None:
    found = gate.read_gate(conn, campaign_id)
    return None if found is None else found.paused_reason


def _next_step(conn: Connection, campaign_id: UUID, stage: str) -> dict[str, str] | None:
    if stage == "closed":
        return None
    key = conn.execute(
        text(
            """SELECT step_key FROM campaign_steps WHERE campaign_id = :c
                 AND status IN ('pending', 'running', 'waiting') ORDER BY due_at, created_at LIMIT 1"""
        ),
        {"c": campaign_id},
    ).scalar_one_or_none()
    key = key or "research"
    role, line = _NEXT_TEXT.get(key.split(":")[0], _NEXT_TEXT["research"])
    return {"step_key": key, "agent_role": role, "text": line}


def summary(conn: Connection, campaign_id: UUID) -> dict[str, Any]:
    """API 3.7's campaign shape, as every write endpoint but pause and cancel answers it."""
    row = _row(conn, campaign_id)
    return {
        "id": row.id,
        "job": {"id": row.job_id, "title": row.title, "company_name": row.company_name},
        "stage": row.stage,
        "paused_at": row.paused_at,
        "pause_reason": _pause_reason(conn, campaign_id) if row.paused_at else None,
        "closed_reason": row.closed_reason,
        "budget_usd": usd_text(row.budget_microusd),
        "spent_usd": usd_text(guardian.spent_microusd(conn, campaign_id=campaign_id), places=6),
        "follow_up_after_days": row.follow_up_after_days,
        "follow_up_email": row.follow_up_email,
        "next_step": _next_step(conn, campaign_id, row.stage),
        "created_at": row.created_at,
    }


# ---- starting -------------------------------------------------------------------------------------------------------


def _insert(
    conn: Connection,
    user_id: UUID,
    job_id: int,
    *,
    stage: str,
    budget: int,
    match_decision_id: UUID | None = None,
    introduction_id: UUID | None = None,
) -> UUID | None:
    """The campaign row, or None when the person already has an open one for the job (the one-open-per-job index)."""
    return conn.execute(
        text(
            """INSERT INTO campaigns (user_id, job_id, match_decision_id, introduction_id, stage, budget_microusd)
               VALUES (:u, :j, :m, :i, CAST(:stage AS campaign_stage), :b)
               ON CONFLICT (user_id, job_id) WHERE stage <> 'closed' DO NOTHING RETURNING id"""
        ),
        {"u": user_id, "j": job_id, "m": match_decision_id, "i": introduction_id, "stage": stage, "b": budget},
    ).scalar_one_or_none()


def _open_for_job(conn: Connection, user_id: UUID, job_id: int) -> UUID | None:
    return conn.execute(
        text("SELECT id FROM campaigns WHERE user_id = :u AND job_id = :j AND stage <> 'closed'"),
        {"u": user_id, "j": job_id},
    ).scalar_one_or_none()


def _link_application(conn: Connection, user_id: UUID, job_id: int, campaign_id: UUID) -> None:
    """A status the person recorded before the campaign existed belongs to it from now on."""
    application = apply.open_application_for_job(conn, user_id, job_id)
    if application is not None:
        apply.link_campaign(conn, application.id, campaign_id)


def start(conn: Connection, user_id: UUID, job_id: int, *, budget_microusd: int | None = None) -> UUID:
    """Approve a match: a `researching` campaign for the job and the job that wakes it (CMP-01). Refusals, in order:
    404 no such job, 403 no AI-processing consent, 410 the job is closed, 422 it breaks a hard constraint, 409 an open
    campaign exists."""
    job = ingest.get_job(conn, job_id)
    if job is None:
        raise ApiError(404, "not_found", "No such resource.")
    trust.require_consent(conn, user_id, "ai_processing")
    if job["status"] != "open":
        raise ApiError(410, "job_closed", "This job is no longer open.")
    verdict = match.decision_for_job(conn, user_id, job_id)
    if verdict is not None and verdict.outcome == "filtered":
        raise ApiError(
            422,
            "violates_constraint",
            "This job breaks one of your hard constraints.",
            {"kinds": list(verdict.violated_kinds)},
        )
    shown = conn.execute(
        text(
            """SELECT id FROM match_decisions WHERE user_id = :u AND job_id = :j AND outcome = 'shown'
               ORDER BY created_at DESC LIMIT 1"""
        ),
        {"u": user_id, "j": job_id},
    ).scalar_one_or_none()
    budget = budget_microusd or config.get().campaign_budget_microusd
    created = _insert(conn, user_id, job_id, stage="researching", budget=budget, match_decision_id=shown)
    if created is None:
        raise ApiError(
            409,
            "conflict",
            "A campaign for this job is already open.",
            {"campaign_id": str(_open_for_job(conn, user_id, job_id))},
        )
    _link_application(conn, user_id, job_id, created)
    stages.enqueue_wake(conn, created, user_id, "start")
    return created


def from_introduction(conn: Connection, user_id: UUID, introduction_id: UUID) -> UUID | None:
    """The campaign an accepted introduction starts, at `applied` (the candidate applied by accepting). Runs on the
    worker's connection acting for the candidate, as the `introduction.accepted` job. Safe to run again: it makes no
    second campaign, and an open campaign for the same job takes the introduction instead of a new one. None when the
    introduction is not this person's, is not accepted or names no job."""
    ref = hiring.introduction_ref(conn, introduction_id)
    if ref is None or ref.user_id != user_id or ref.status != "accepted" or ref.job_id is None:
        return None
    started = conn.execute(
        text("SELECT id FROM campaigns WHERE user_id = :u AND introduction_id = :i"),
        {"u": user_id, "i": introduction_id},
    ).scalar_one_or_none()
    if started is not None:
        return UUID(str(started))
    existing = _open_for_job(conn, user_id, ref.job_id)
    if existing is not None:
        conn.execute(
            text("UPDATE campaigns SET introduction_id = :i WHERE id = :c AND introduction_id IS NULL"),
            {"i": introduction_id, "c": existing},
        )
        return UUID(str(existing))
    created = _insert(
        conn,
        user_id,
        ref.job_id,
        stage="applied",
        budget=config.get().campaign_budget_microusd,
        introduction_id=introduction_id,
    )
    if created is None:  # another run made one between the read and the insert
        return None
    stages.enqueue_wake(conn, created, user_id, "start")
    return created


# ---- changing -------------------------------------------------------------------------------------------------------

_UNSET: Any = object()


def update(
    conn: Connection,
    user_id: UUID,
    campaign_id: UUID,
    *,
    budget_microusd: int | None = None,
    follow_up_after_days: int | None = None,
    follow_up_email: Any = _UNSET,
) -> dict[str, Any]:
    """Change the budget, the quiet days or the follow-up address (None clears the address). Each change wakes the
    campaign once, with a cause named for what changed and a microsecond stamp (so changing it twice wakes it twice)."""
    row = _row(conn, campaign_id)
    if row.stage == "closed":
        raise _closed()
    changes: list[tuple[str, str]] = []  # (column assignment, cause prefix)
    values: dict[str, Any] = {"id": campaign_id}
    if budget_microusd is not None:
        changes.append(("budget_microusd = :budget", "budget"))
        values["budget"] = budget_microusd
    if follow_up_after_days is not None:
        changes.append(("follow_up_after_days = :days", "days"))
        values["days"] = follow_up_after_days
    if follow_up_email is not _UNSET:
        changes.append(("follow_up_email = :email", "address"))
        values["email"] = follow_up_email
    if changes:
        stamp = conn.execute(
            text(
                f"""UPDATE campaigns SET {", ".join(sql for sql, _ in changes)}
                    WHERE id = :id AND stage <> 'closed'
                    RETURNING CAST(floor(extract(epoch FROM clock_timestamp()) * 1000000) AS bigint)"""
            ),
            values,
        ).scalar_one_or_none()
        if stamp is None:
            raise _closed()
        for number, (_, cause) in enumerate(changes):
            stages.enqueue_wake(conn, campaign_id, user_id, f"{cause}.{stamp + number}")
    return summary(conn, campaign_id)


# ---- pause, resume, cancel ------------------------------------------------------------------------------------------


def _state(conn: Connection, campaign_id: UUID) -> dict[str, Any]:
    row = _row(conn, campaign_id)
    return {
        "id": row.id,
        "stage": row.stage,
        "paused_at": row.paused_at,
        "pause_reason": _pause_reason(conn, campaign_id) if row.paused_at else None,
    }


def pause(conn: Connection, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
    """Pause for the candidate's own reason (CMP-07). A campaign already paused answers with its current state."""
    row = _row(conn, campaign_id)
    if row.stage == "closed":
        raise _closed()
    held = {
        "running_steps_stopping": conn.execute(
            text("SELECT count(*) FROM campaign_steps WHERE campaign_id = :c AND status = 'running'"),
            {"c": campaign_id},
        ).scalar_one(),
        "follow_ups_held": conn.execute(
            text(
                """SELECT count(*) FROM agent_actions
                   WHERE campaign_id = :c AND action_type = 'send_follow_up' AND status = 'scheduled'"""
            ),
            {"c": campaign_id},
        ).scalar_one(),
    }
    for _ in range(CANCEL_ATTEMPTS):
        if row.paused_at is not None or stages.pause(conn, campaign_id, "candidate"):
            break
        row = _row(conn, campaign_id)  # another writer moved it: look again
        if row.stage == "closed":
            raise _closed()
    else:
        raise ApiError(409, "conflict", "The campaign changed while pausing. Try again.", {"state": "busy"})
    state = _state(conn, campaign_id)
    if row.paused_at is not None:  # it was paused already: nothing was held by this call
        return state
    return {
        **state,
        "held": held,
        "effective": "No new step starts and nothing is sent until you resume.",
    }


def resume(conn: Connection, user_id: UUID, campaign_id: UUID) -> dict[str, Any]:
    """Resume a paused campaign, whoever paused it (a scam pause is the candidate's to override). One paused at its
    budget stays paused, `409 budget_exhausted`, until the budget is raised above what is spent."""
    row = _row(conn, campaign_id)
    if row.stage == "closed":
        raise _closed()
    if row.paused_at is not None:
        if _pause_reason(conn, campaign_id) == "budget_exhausted" and (
            guardian.spent_microusd(conn, campaign_id=campaign_id) >= row.budget_microusd
        ):
            raise ApiError(
                409,
                "budget_exhausted",
                "This campaign has spent its budget. Raise the budget to continue.",
                {"budget_usd": usd_text(row.budget_microusd)},
            )
        stages.unpause(conn, campaign_id, by="candidate")
    return _state(conn, campaign_id)


def cancel(
    conn: Connection, user_id: UUID, campaign_id: UUID, *, note: str | None = None, reason: str | None = None
) -> dict[str, Any]:
    """Close the campaign (CMP-07): its pending approvals expire with the note, its open actions and waiting messages
    are cancelled. `reason` `offer_accepted` or `offer_declined` closes an offer-stage campaign with that reason; the
    introduction behind it is not touched (the employer still owes its answer)."""
    row = _row(conn, campaign_id)
    if row.stage == "closed":
        raise ApiError(409, "conflict", "This campaign is already closed.", {"state": "closed"})
    if reason in OFFER_REASONS and row.stage != "offer":
        raise ApiError(
            422,
            "validation_failed",
            "That reason is only for a campaign at the offer stage.",
            {"fields": [{"field": "reason", "issue": "only_at_offer"}]},
        )
    closing = reason or "cancelled"
    for _ in range(CANCEL_ATTEMPTS):
        if stages.move(conn, campaign_id, "closed", cause="cancel", closed_reason=closing):
            break
        if _row(conn, campaign_id).stage == "closed":
            raise ApiError(409, "conflict", "This campaign is already closed.", {"state": "closed"})
    else:
        raise ApiError(409, "conflict", "The campaign changed while cancelling. Try again.", {"state": "busy"})
    counts = guardian.cancel_open(conn, campaign_id, note or DEFAULT_CANCEL_NOTE)
    closed = _row(conn, campaign_id)
    return {
        "id": closed.id,
        "stage": closed.stage,
        "closed_reason": closed.closed_reason,
        "closed_at": closed.closed_at,
        "expired_approvals": counts["approvals_expired"],
        "cancelled_actions": counts["actions_cancelled"],
        "introduction_id": closed.introduction_id,
    }
