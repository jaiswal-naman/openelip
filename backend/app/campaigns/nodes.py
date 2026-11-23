"""The campaign graph's nodes (ARCHITECTURE 5.9; AI_SYSTEM 6.2): `route` and the six work nodes.

`route` is the only node that reads the campaign as a whole and the only code in the graph that moves a stage (through
`campaigns.stages.move`, under `lock_version`). It looks at the tables and names the next node, so a fresh thread for an
open campaign lands where the tables say. Every work node opens its step row first, returns at once when the step is
already ended, writes the ids of what it made in the transaction that makes it, and ends by returning to `route`.
Nodes run at least once: a crash after a commit re-runs the node, which finds its committed work and skips it.

A node never calls `interrupt`; only `park` does. A node that must wait leaves its step `waiting` (for time, for the
candidate or for an approval) and `route` parks the thread until a queue job resumes it.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from sqlalchemy import Connection, text

from app.apply import api as apply
from app.guardian import api as guardian
from app.hiring import api as hiring
from app.ingest import api as ingest
from app.match import api as match
from app.platform import config, db, llm, log, queue
from app.platform.errors import ApiError
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid
from app.platform.queue import NonRetryable
from app.profile import api as profile
from app.tailor import api as tailor

from . import board, gate, research_loop, stages
from .crew import chaser, coach, common, fit, research

END = "end"
WORK_NODES = ("research", "fit", "packet_draft", "packet", "follow_up", "interview_prep")

# assumption: AI_SYSTEM 6.2 per-node cost estimates in micro-dollars; a model-using node whose estimate would pass the
# budget pauses the campaign instead of running (GRD-07).
ESTIMATE_MICROUSD = {"research": 300_000, "packet_draft": 50_000, "follow_up": 10_000, "interview_prep": 40_000}
MAX_FOLLOW_UPS = 2  # CMP-04: never a third
MAX_FACTS = 60  # assumption: confirmed facts a prompt carries
WITHHELD = "(withheld: the page this came from looked like an instruction)"
RANK = {"researching": 0, "needs_you": 1, "applied": 2, "talking": 3, "interview": 4, "offer": 5}
APPLICATION_STAGES = ("applied", "interview", "offer")
REGENERATE_CODES = ("campaign_paused",)  # a blocked action with this outcome is proposed again after the resume


class State(TypedDict):
    """The graph state: ids and the cause, never text (ARCHITECTURE 5.9 rule 2)."""

    campaign_id: str
    next: str | None
    cause: str | None


def thread_id(campaign_id: UUID | str) -> str:
    return f"campaign:{campaign_id}"


# ---- reading the campaign ----------


@dataclass(frozen=True)
class Campaign:
    id: UUID
    user_id: UUID
    job_id: int
    stage: str
    paused: bool
    active: bool
    introduction_id: UUID | None
    follow_up_email: str | None
    follow_up_after_days: int
    last_update_at: datetime
    job_status: str
    job_title: str
    job_text: str
    content_version: int


def read_campaign(conn: Connection, campaign_id: UUID) -> Campaign | None:
    row = conn.execute(
        text(
            """SELECT c.id, c.user_id, c.job_id, c.stage::text AS stage, c.paused_at IS NOT NULL AS paused,
                      u.status::text = 'active' AS active, c.introduction_id, c.follow_up_email,
                      c.follow_up_after_days, c.last_update_at, j.status AS job_status, j.title, j.description,
                      j.content_version
               FROM campaigns c JOIN users u ON u.id = c.user_id JOIN jobs j ON j.id = c.job_id WHERE c.id = :id"""
        ),
        {"id": campaign_id},
    ).one_or_none()
    if row is None:
        return None
    return Campaign(
        row.id,
        row.user_id,
        row.job_id,
        row.stage,
        row.paused,
        row.active,
        row.introduction_id,
        row.follow_up_email,
        row.follow_up_after_days,
        row.last_update_at,
        row.job_status,
        row.title,
        row.description,
        row.content_version,
    )


@dataclass(frozen=True)
class ActionInfo:
    id: int
    status: str
    outcome_code: str | None
    approval_id: UUID | None


def current_action(conn: Connection, campaign_id: UUID, first_id: int | None) -> ActionInfo | None:
    """The action of a step as it stands now: the one the step proposed, or its replacement when the candidate edited it
    (the replacement names the original in `inputs.replaces`)."""
    if first_id is None:
        return None
    current = first_id
    for _ in range(10):  # a chain of edits is short
        nxt = conn.execute(
            text(
                """SELECT id FROM agent_actions WHERE campaign_id = :c AND inputs ->> 'replaces' = :cur
                   ORDER BY id LIMIT 1"""
            ),
            {"c": campaign_id, "cur": str(current)},
        ).scalar_one_or_none()
        if nxt is None:
            break
        current = int(nxt)
    row = conn.execute(
        text(
            """SELECT a.id, a.status::text AS status, a.outcome_code, p.id AS approval_id FROM agent_actions a
               LEFT JOIN approvals p ON p.agent_action_id = a.id WHERE a.id = :id"""
        ),
        {"id": current},
    ).one_or_none()
    return None if row is None else ActionInfo(row.id, row.status, row.outcome_code, row.approval_id)


def _action_of(conn: Connection, campaign_id: UUID, step: board.Step | None) -> ActionInfo | None:
    raw = None if step is None else step.output.get("action_id")
    return current_action(conn, campaign_id, None if raw is None else int(raw))


def _regenerates(action: ActionInfo | None) -> bool:
    """An action that never reached the candidate's decision, or whose approval lapsed: it is proposed again."""
    return action is not None and (
        action.status == "expired" or (action.status == "blocked" and action.outcome_code in REGENERATE_CODES)
    )


def _generation(conn: Connection, campaign_id: UUID, action_type: str, key: str) -> int:
    """How many earlier proposals of this step were `regenerates`: the step id of a proposal carries it, so a proposal
    made again after a pause is a new action and a re-run of the same one finds the old."""
    return int(
        conn.execute(
            text(
                """SELECT count(*) FROM agent_actions
                   WHERE campaign_id = :c AND action_type = CAST(:t AS action_type) AND inputs ->> 'step_id' LIKE :key
                     AND (status = 'expired' OR (status = 'blocked' AND outcome_code = ANY(:codes)))"""
            ),
            {"c": campaign_id, "t": action_type, "key": f"{key}.%", "codes": list(REGENERATE_CODES)},
        ).scalar_one()
    )


# ---- follow-up timing (pure; route and the node share it) ----------


@dataclass(frozen=True)
class FollowUpPlan:
    todo: str  # `node` (run the follow_up node), `park` or `none` (both follow-ups are over)
    n: int = 0
    due: datetime | None = None


def follow_up_plan(
    steps: Mapping[str, board.Step],
    actions: Mapping[str, ActionInfo | None],
    *,
    last_update_at: datetime,
    days: int,
    now: datetime,
    has_address: bool,
) -> FollowUpPlan:
    """What the Chaser does next: `follow_up:1` is due `days` after the last news, `follow_up:2` the same after
    `follow_up:1` ended (or after later news), and there is never a third (CMP-04)."""
    previous_end: datetime | None = None
    for n in range(1, MAX_FOLLOW_UPS + 1):
        step = steps.get(f"follow_up:{n}")
        if step is not None and step.ended:
            previous_end = step.finished_at
            continue
        base = max(last_update_at, previous_end) if previous_end is not None else last_update_at
        due = base + timedelta(days=days)
        if step is None or step.status != "waiting":
            return FollowUpPlan("node", n, due)
        action = actions.get(step.step_key)
        if step.waiting_for_approval_id is not None or "action_id" in step.output:
            if action is None or action.status == "awaiting_approval":
                return FollowUpPlan("park", n, due)
            if action.status == "rejected" and action.outcome_code == "edited":
                return FollowUpPlan("park", n, due)  # the replacement is on its way
            return FollowUpPlan("node", n, due)  # decided: the node ends the step or proposes it again
        if step.output.get("todo") == "add_follow_up_address":
            return FollowUpPlan("node" if has_address else "park", n, due)
        if step.due_at != due or due <= now:
            return FollowUpPlan("node", n, due)
        return FollowUpPlan("park", n, due)
    return FollowUpPlan("none")


# ---- route ----------


def _owner(campaign_id: UUID) -> UUID | None:
    with db.transaction(role="worker") as conn:
        found = conn.execute(
            text("SELECT user_id FROM campaigns WHERE id = :id"), {"id": campaign_id}
        ).scalar_one_or_none()
    return None if found is None else UUID(str(found))


def route(state: "State") -> dict[str, Any]:
    """Read the tables and name the next node (`research`, `fit`, `packet_draft`, `packet`, `follow_up`,
    `interview_prep`, `park`) or `end`. The only place a stage is changed."""
    campaign_id = UUID(state["campaign_id"])
    user_id = _owner(campaign_id)
    if user_id is None:
        return {"next": END}
    for _ in range(3):  # a move that lost the lock-version race is decided again from fresh rows
        with db.transaction(user_id, role="worker") as conn:
            chosen = _route(conn, campaign_id)
        if chosen is not None:
            return {"next": chosen}
    return {"next": "park"}


def _end(conn: Connection, campaign: Campaign) -> str:
    queue.enqueue(
        conn,
        "graph.delete",
        {"thread_id": thread_id(campaign.id)},
        unique_key=f"graph.delete:{thread_id(campaign.id)}",
        user_id=campaign.user_id,
    )
    return END


def _route(conn: Connection, campaign_id: UUID) -> str | None:
    """One decision. None means a stage move lost a race: decide again."""
    c = read_campaign(conn, campaign_id)
    if c is None:
        return END
    if c.stage == "closed":
        return _end(conn, c)
    if c.paused or not c.active:
        return "park"
    stage = c.stage

    def move(to: str, cause: str, reason: str | None = None) -> bool:
        return stages.move(conn, campaign_id, to, cause=cause, closed_reason=reason, wake=False)

    # a closed job ends the campaign before the application is made
    if stage in ("researching", "needs_you") and c.job_status == "closed":
        return _end(conn, c) if move("closed", "job_closed", "job_closed") else None
    # a scam signal in the posting pauses it before anything is researched, once per content version (decision 24)
    if stage == "researching":
        found = guardian.scam_signals(f"{c.job_title}\n{c.job_text}")
        if found and not _flagged_before(conn, campaign_id, c.content_version):
            paused = stages.pause(
                conn,
                campaign_id,
                "scam_suspected",
                details={
                    "signals": [{"kind": s.kind, "quote": s.quote} for s in found],
                    "content_version": c.content_version,
                },
                wake=False,
            )
            return "park" if paused else None
    steps = board.steps_of(conn, campaign_id)
    intro = c.introduction_id is not None
    # what the candidate and the employer have recorded moves the stage (never backwards)
    application = apply.application_for_campaign(conn, campaign_id)
    status = None if application is None else application.status
    if status in ("rejected", "no_response"):
        return _end(conn, c) if move("closed", f"application_{status}", status) else None
    submit = _action_of(conn, campaign_id, steps.get("packet"))
    packet_id = steps["packet_draft"].output.get("packet_id") if "packet_draft" in steps else None
    if submit is not None and submit.status in ("scheduled", "executed") and packet_id:
        apply.mark_packet_approved(conn, UUID(str(packet_id)))
    if status in APPLICATION_STAGES:
        if submit is not None and submit.status == "scheduled":
            guardian.mark_executed(conn, submit.id)
        if RANK[status] > RANK[stage]:
            if not move(status, f"application_{status}"):
                return None
            stage = status
    if intro and stage == "applied":
        ref = hiring.introduction_ref(conn, c.introduction_id) if c.introduction_id else None
        if ref is not None and ref.employer_answer == "yes":
            if not move("talking", "employer_yes"):
                return None
            stage = "talking"
        elif ref is not None and ref.employer_answer == "no":
            return _end(conn, c) if move("closed", "employer_no", "rejected") else None
    if stage == "researching" and submit is not None and submit.status == "awaiting_approval":
        if not move("needs_you", "packet_waiting"):
            return None
        stage = "needs_you"
    node = _next_node(conn, c, stage, steps, submit, intro)
    if node in ESTIMATE_MICROUSD:
        subject = gate.read_gate(conn, campaign_id)
        if subject is not None and not guardian.budget_ok(conn, subject, ESTIMATE_MICROUSD[node]):
            return "park" if stages.pause(conn, campaign_id, "budget_exhausted", wake=False) else None
    return node


def _flagged_before(conn: Connection, campaign_id: UUID, content_version: int) -> bool:
    return (
        conn.execute(
            text(
                """SELECT 1 FROM agent_actions WHERE campaign_id = :c AND kind = 'state_change'
                   AND outcome_code = 'scam_suspected' AND inputs ->> 'content_version' = :v LIMIT 1"""
            ),
            {"c": campaign_id, "v": str(content_version)},
        ).first()
        is not None
    )


def _ended(steps: Mapping[str, board.Step], key: str) -> bool:
    return key in steps and steps[key].ended


def _next_node(
    conn: Connection,
    c: Campaign,
    stage: str,
    steps: Mapping[str, board.Step],
    submit: ActionInfo | None,
    intro: bool,
) -> str:
    if not _ended(steps, "research"):
        return "research"
    if stage in ("researching", "needs_you") and not intro:
        if not _ended(steps, "fit"):
            return "fit"
        if not _ended(steps, "packet_draft"):
            return "packet_draft"
        if not _ended(steps, "packet"):
            step = steps.get("packet")
            if step is None or step.status != "waiting" or "action_id" not in step.output:
                return "packet"
            if submit is None or submit.status == "awaiting_approval":
                return "park"
            if submit.status in ("scheduled", "executed") or _regenerates(submit):
                return "packet"
            return "park"  # rejected, cancelled or refused: the candidate edits it (a new action) or cancels
    if stage == "applied" and not intro:
        actions = {key: _action_of(conn, c.id, step) for key, step in steps.items() if key.startswith("follow_up:")}
        plan = follow_up_plan(
            steps,
            actions,
            last_update_at=c.last_update_at,
            days=c.follow_up_after_days,
            now=datetime.now(UTC),
            has_address=c.follow_up_email is not None,
        )
        return "follow_up" if plan.todo == "node" else "park"
    if stage == "interview" and not _ended(steps, "interview_prep"):
        return "interview_prep"
    return "park"


def describe(campaign_id: str) -> dict[str, Any]:
    """What `park` waits for: a pending approval, a time, or an event (a status, an answer, the candidate)."""
    cid = UUID(campaign_id)
    with db.transaction(role="worker") as conn:
        approval = conn.execute(
            text(
                """SELECT p.id FROM approvals p JOIN agent_actions a ON a.id = p.agent_action_id
                   WHERE a.campaign_id = :c AND p.status = 'pending' ORDER BY p.requested_at LIMIT 1"""
            ),
            {"c": cid},
        ).scalar_one_or_none()
        if approval is not None:
            return {"waiting_for": "approval", "ref": str(approval)}
        due = conn.execute(
            text(
                """SELECT min(due_at) FROM campaign_steps WHERE campaign_id = :c AND status = 'waiting'
                   AND waiting_for_approval_id IS NULL AND due_at > now()"""
            ),
            {"c": cid},
        ).scalar_one_or_none()
        if due is not None:
            return {"waiting_for": "time", "ref": due.isoformat()}
        stage = conn.execute(text("SELECT stage::text FROM campaigns WHERE id = :c"), {"c": cid}).scalar_one_or_none()
    return {"waiting_for": "event", "ref": stage or "none"}


# ---- shared by the work nodes ----------


@dataclass(frozen=True)
class Ctx:
    campaign_id: UUID
    user_id: UUID
    job_id: int
    step: board.Step
    run_id: UUID


def _tx(user_id: UUID) -> Any:
    return db.transaction(user_id, role="worker")


def _enter(state: Mapping[str, Any], step_key: str, role: str) -> Ctx | None:
    """Open the node's step and its agent run (the run of an unfinished step is reused, so a re-run proposes into the
    same run and the proposal's step id finds what was already made). None when the step is already ended."""
    from .graph import GRAPH_VERSION  # graph imports this module; the version lives with the graph

    campaign_id = UUID(state["campaign_id"])
    user_id = _owner(campaign_id)
    if user_id is None:
        return None
    with _tx(user_id) as conn:
        campaign = read_campaign(conn, campaign_id)
        if campaign is None or campaign.stage == "closed":
            return None
        step = board.open_step(conn, campaign_id, step_key, role)
        if step.ended:
            return None
        board.start(conn, step)
        run_id = conn.execute(
            text(
                """SELECT id FROM agent_runs WHERE campaign_step_id = :s AND status = 'running'
                   ORDER BY started_at LIMIT 1"""
            ),
            {"s": step.id},
        ).scalar_one_or_none()
        if run_id is None:
            run_id = guardian.open_run(conn, user_id, campaign_id, role, GRAPH_VERSION, campaign_step_id=step.id)
        return Ctx(campaign_id, user_id, campaign.job_id, step, UUID(str(run_id)))


def _close(conn: Connection, ctx: Ctx, *, status: str = "succeeded", code: str | None = None) -> None:
    guardian.close_run(conn, ctx.run_id, status=status, error_code=code)


def _fail(ctx: Ctx, code: str, output: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """End the step as failed for a reason a retry cannot fix; the graph goes back to route."""
    with _tx(ctx.user_id) as conn:
        board.fail(conn, ctx.step.id, code, output)
        _close(conn, ctx, status="failed", code=code)
    log.event("campaign.step_failed", campaign_id=ctx.campaign_id, step_key=ctx.step.step_key, error_code=code)
    return {"next": None}


def _model_row(conn: Connection, ctx: Ctx, summary: str, call_ids: Sequence[int], task: str) -> None:
    if call_ids:
        guardian.record(
            conn, ctx.run_id, kind="model_call", summary=summary, inputs={"task": task, "llm_call_ids": list(call_ids)}
        )


def _facts(conn: Connection, user_id: UUID) -> list[tailor.Fact]:
    return [tailor.to_fact(row) for row in profile.usable_facts(conn, user_id)][:MAX_FACTS]


def _terms(conn: Connection, user_id: UUID, job: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    skills = [
        str(f["attributes"].get("name") or f["statement"])
        for f in profile.usable_facts(conn, user_id)
        if f["kind"] == "skill"
    ]
    return skills, [str(job["title"]), *(str(r["text"]) for r in job.get("requirements", []))]


def _requirements(job: Mapping[str, Any]) -> list[tailor.Requirement]:
    return [tailor.Requirement(int(r["id"]), str(r["text"]), str(r["importance"])) for r in job.get("requirements", [])]


def _dossier(conn: Connection, campaign_id: UUID) -> tuple[UUID | None, list[dict[str, Any]]]:
    row = conn.execute(
        text("SELECT id, claims FROM company_dossiers WHERE campaign_id = :c ORDER BY version DESC LIMIT 1"),
        {"c": campaign_id},
    ).one_or_none()
    return (None, []) if row is None else (UUID(str(row.id)), list(row.claims))


def _model_view(claims: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The claims as a model may read them: the same list and order (so an index means the same thing everywhere), with
    the text of a claim from a page that looked like an injection withheld."""
    return [{**c, "text": WITHHELD} if c.get("injection_suspected") else dict(c) for c in claims]


def _decision(conn: Connection, user_id: UUID, job_id: int) -> tuple[Sequence[Any], dict[int, str]]:
    view = match.decision_for_job(conn, user_id, job_id)
    rows = () if view is None else view.evidence
    return rows, {row.requirement_id: row.verdict for row in rows}


# ---- research (the Researcher) ----------


def research_node(state: "State") -> dict[str, Any]:
    ctx = _enter(state, "research", "researcher")
    if ctx is None:
        return {"next": None}
    with _tx(ctx.user_id) as conn:
        existing, _ = _dossier(conn, ctx.campaign_id)
        if existing is not None:  # the dossier was stored and the step end was not: finish the step, run nothing
            board.complete(conn, ctx.step.id, {"dossier_id": existing})
            _close(conn, ctx)
            return {"next": None}
        job = ingest.get_job(conn, ctx.job_id) or {}
    hosts = research.allowed_hosts(job.get("apply_url"), job.get("apply_url"))
    directory = config.get().research_pages_dir
    source = research.replay_source(Path(directory), hosts) if directory else research.live_source(hosts)
    work = research_loop.ResearchJob(str(job.get("company_name", "")), hosts, job.get("apply_url"), source)

    def on_tool_call(record: research_loop.ToolRecord) -> None:
        with _tx(ctx.user_id) as conn:
            guardian.record(
                conn,
                ctx.run_id,
                kind="tool_call",
                summary=f"Fetched a public page: {record.outcome}.",
                inputs={
                    "tool": record.tool,
                    "url": record.url,
                    "content_sha256": record.content_sha256,
                    "at": record.at.isoformat(),
                    "outcome": record.outcome,
                },
                outcome_code=None if record.outcome == "ok" else record.outcome,
            )

    try:
        with llm.attributed(campaign_id=ctx.campaign_id, agent_run_id=ctx.run_id) as used:

            def tokens() -> int:
                if not used.call_ids:
                    return 0
                with db.transaction(role="worker") as conn:
                    return int(
                        conn.execute(
                            text(
                                """SELECT COALESCE(sum(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)
                                   FROM llm_calls WHERE id = ANY(CAST(:ids AS bigint[]))"""
                            ),
                            {"ids": list(used.call_ids)},
                        ).scalar_one()
                    )

            result = research_loop.run(work, principal=ctx.user_id, on_tool_call=on_tool_call, tokens=tokens)
    except AIDisabled:
        return _fail(ctx, "ai_disabled")
    with _tx(ctx.user_id) as conn:
        conn.execute(
            text(
                """INSERT INTO company_dossiers (campaign_id, user_id, company_name, version, claims, agent_run_id)
                   VALUES (:c, :u, :name, 1, CAST(:claims AS jsonb), :run)
                   ON CONFLICT (campaign_id, version) DO NOTHING"""
            ),
            {
                "c": ctx.campaign_id,
                "u": ctx.user_id,
                "name": work.company_name,
                "claims": json.dumps(result.claims, default=str),
                "run": ctx.run_id,
            },
        )
        dossier_id, _ = _dossier(conn, ctx.campaign_id)
        _model_row(conn, ctx, "Planned and read the company's pages.", used.call_ids, "research")
        board.complete(
            conn,
            ctx.step.id,
            {
                "dossier_id": dossier_id,
                "incomplete": result.incomplete,
                "failures": [{"url": url, "code": code} for url, code in result.failures],
            },
        )
        _close(conn, ctx)
    return {"next": None}


# ---- fit (the Analyst) ----------


def fit_node(state: "State") -> dict[str, Any]:
    ctx = _enter(state, "fit", "analyst")
    if ctx is None:
        return {"next": None}
    with _tx(ctx.user_id) as conn:
        job = ingest.get_job(conn, ctx.job_id) or {}
        rows, _ = _decision(conn, ctx.user_id, ctx.job_id)
        result = fit.analyse(rows, _requirements(job))
        guardian.record(
            conn,
            ctx.run_id,
            kind="tool_call",
            summary="Grouped the requirements by their stored verdicts.",
            inputs={
                name: len(result[name]) for name in ("met", "partly_met", "not_met", "no_evidence", "not_assessed")
            },
        )
        board.complete(conn, ctx.step.id, result)
        _close(conn, ctx)
    return {"next": None}


# ---- packet_draft (the Writer) ----------


def packet_draft_node(state: "State") -> dict[str, Any]:
    ctx = _enter(state, "packet_draft", "writer")
    if ctx is None:
        return {"next": None}
    try:
        with llm.attributed(campaign_id=ctx.campaign_id, agent_run_id=ctx.run_id) as used:
            packet_id = _draft_packet(ctx)
    except ProviderUnavailable:
        raise  # the queue retries the job; the ids already noted are reused
    except NonRetryable as failure:
        return _fail(ctx, str(failure) or "packet_failed")
    except ApiError as failure:
        return _fail(ctx, failure.code)
    with _tx(ctx.user_id) as conn:
        view = apply.get_packet(conn, ctx.user_id, packet_id)
        if view.status not in ("ready", "approved"):
            board.fail(conn, ctx.step.id, f"packet_{view.status}")
            _close(conn, ctx, status="failed", code=f"packet_{view.status}")
            return {"next": None}
        _model_row(conn, ctx, "Drafted the packet answers and note.", used.call_ids, "writer.packet")
        board.complete(conn, ctx.step.id)
        _close(conn, ctx)
    return {"next": None}


def _draft_packet(ctx: Ctx) -> UUID:
    """A tailored resume and a campaign packet, each made once (their ids are noted at once) and then drafted with the
    dossier's claims. A re-run reuses what exists."""
    with _tx(ctx.user_id) as conn:
        noted = board.get_step(conn, ctx.campaign_id, "packet_draft")
        output = {} if noted is None else noted.output
        tailored = output.get("tailored_resume_id")
        packet = output.get("packet_id")
        if tailored is None and packet is None:
            found = tailor.latest_ready_for_job(conn, ctx.user_id, ctx.job_id)
            if found is None:
                try:
                    found = UUID(str(tailor.request(conn, ctx.user_id, ctx.job_id).id))
                except ApiError as failure:
                    if failure.code in ("job_closed", "not_found"):
                        raise
                    found = None  # no resume or no capacity: the packet is made without a tailored one
            board.note(conn, ctx.step.id, {"tailored_resume_id": found})
            tailored = found
    if tailored is not None and packet is None:
        tailor.generate(UUID(str(tailored)))  # a no-op unless it is still generating
    with _tx(ctx.user_id) as conn:
        _, claims = _dossier(conn, ctx.campaign_id)
        if packet is None:
            view = apply.request_packet(
                conn,
                ctx.user_id,
                ctx.job_id,
                None if tailored is None else UUID(str(tailored)),
                campaign_id=ctx.campaign_id,
            )
            packet = view.id
            board.note(conn, ctx.step.id, {"packet_id": packet})
    with db.transaction(ctx.user_id, role="worker") as conn:  # draft commits inside, so it gets a connection of its own
        apply.draft_packet(conn, {"packet_id": str(packet)}, dossier_claims=_model_view(claims) or None)
    return UUID(str(packet))


# ---- packet (the Operator role) ----------


def packet_node(state: "State") -> dict[str, Any]:
    ctx = _enter(state, "packet", "operator")
    if ctx is None:
        return {"next": None}
    with _tx(ctx.user_id) as conn:
        step = board.get_step(conn, ctx.campaign_id, "packet")
        assert step is not None
        action = _action_of(conn, ctx.campaign_id, step)
        if action is not None and action.status in ("scheduled", "executed"):
            board.complete(conn, ctx.step.id)  # approved and released: the packet step is done
            _close(conn, ctx)
            return {"next": None}
        if action is not None and not _regenerates(action):
            return {"next": None}  # still with the candidate
        drafted = board.get_step(conn, ctx.campaign_id, "packet_draft")
        packet_id = None if drafted is None else drafted.output.get("packet_id")
        if packet_id is None:
            return _fail_in(conn, ctx, "no_packet")
        # the apply link must still be live; a closed job is closed by the check and route then closes the campaign
        if ingest.verify_live(conn, ctx.job_id) is False:
            return _fail_in(conn, ctx, "job_closed")
        job = ingest.get_job(conn, ctx.job_id) or {}
        view = apply.get_packet(conn, ctx.user_id, UUID(str(packet_id)))
        raw = conn.execute(
            text("SELECT answers FROM application_packets WHERE id = :id"), {"id": UUID(str(packet_id))}
        ).scalar_one()
        dossier_id, _ = _dossier(conn, ctx.campaign_id)
        payload, provenance = packet_proposal(
            view, raw, job, dossier_id, [str(f["id"]) for f in profile.usable_facts(conn, ctx.user_id)]
        )
        generation = _generation(conn, ctx.campaign_id, "submit_application", "packet")
        board.propose(
            conn,
            step,
            ctx.run_id,
            action_type="submit_application",
            payload=payload,
            provenance=provenance,
            step_id=f"packet.{generation}",
        )
    return {"next": None}


def _fail_in(conn: Connection, ctx: Ctx, code: str) -> dict[str, Any]:
    board.fail(conn, ctx.step.id, code)
    _close(conn, ctx, status="failed", code=code)
    return {"next": None}


def packet_proposal(
    view: Any,
    raw_answers: Sequence[Mapping[str, Any]],
    job: Mapping[str, Any],
    dossier_id: UUID | None,
    usable_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The `submit_application` payload (INDEX "Actions") and its provenance. Each drafted answer is one cited item: the
    facts it rests on and the dossier claims the Writer cited. The cover note is cited to the confirmed facts the Writer
    was given (the packet stores no note-level citation; the Phase 5 checks already held it sentence by sentence)."""
    claim_indexes = {a["question_id"]: list(a.get("dossier_claim_indexes", [])) for a in raw_answers}
    answers = [
        {"question_id": a.question_id, "question": a.question, "status": a.status, "answer": a.answer}
        for a in view.answers
    ]
    payload = {
        "job_id": view.job_id,
        "apply_url": job.get("apply_url"),
        "packet_id": str(view.id),
        "tailored_resume_id": None if view.tailored_resume_id is None else str(view.tailored_resume_id),
        "answers": answers,
        "cover_note": view.cover_note or "",
    }
    fields: dict[str, str] = {}
    sentences: list[dict[str, Any]] = []
    for a in view.answers:
        if a.answer.strip():
            field = f"answers.{a.question_id}"
            fields[field] = "model"
            sentences.append(
                {
                    "field": field,
                    "text": a.answer,
                    "fact_ids": [str(i) for i in a.fact_ids],
                    "claim_indexes": claim_indexes.get(a.question_id, []),
                }
            )
    if (view.cover_note or "").strip():
        fields["cover_note"] = "model"
        sentences.append(
            {
                "field": "cover_note",
                "text": view.cover_note,
                "fact_ids": list(usable_ids),
                "claim_indexes": sorted({i for v in claim_indexes.values() for i in v}),
            }
        )
    provenance = {
        "fields": fields,
        "sentences": sentences,
        "code_parts": [],
        "dossier_id": None if dossier_id is None else str(dossier_id),
        "input_flags": [],
    }
    return payload, provenance


# ---- follow_up (the Chaser) ----------


def follow_up_node(state: "State") -> dict[str, Any]:
    campaign_id = UUID(state["campaign_id"])
    user_id = _owner(campaign_id)
    if user_id is None:
        return {"next": None}
    with _tx(user_id) as conn:
        c = read_campaign(conn, campaign_id)
        if c is None or c.stage != "applied":
            return {"next": None}
        steps = board.steps_of(conn, campaign_id)
        actions = {k: _action_of(conn, campaign_id, s) for k, s in steps.items() if k.startswith("follow_up:")}
        plan = follow_up_plan(
            steps,
            actions,
            last_update_at=c.last_update_at,
            days=c.follow_up_after_days,
            now=datetime.now(UTC),
            has_address=c.follow_up_email is not None,
        )
    if plan.todo != "node":
        return {"next": None}
    key = f"follow_up:{plan.n}"
    ctx = _enter({"campaign_id": str(campaign_id)}, key, "chaser")
    if ctx is None:
        return {"next": None}
    action = actions.get(key)
    if action is not None:
        return _settle_follow_up(ctx, c, action)
    assert plan.due is not None
    if plan.due > datetime.now(UTC):
        with _tx(user_id) as conn:  # not yet: wait for the day, with the wake job
            board.wait_until(conn, ctx.step, user_id, plan.due)
        return {"next": None}
    if c.follow_up_email is None:
        with _tx(user_id) as conn:
            board.wait_for_person(conn, ctx.step.id, {"todo": "add_follow_up_address"})
        return {"next": None}
    return _draft_follow_up(ctx, c, plan.n)


def _settle_follow_up(ctx: Ctx, c: Campaign, action: ActionInfo) -> dict[str, Any]:
    """The candidate has decided (or the proposal lapsed): end the step, or propose the same message again."""
    with _tx(ctx.user_id) as conn:
        if action.status in ("scheduled", "executed"):
            board.complete(conn, ctx.step.id)
        elif _regenerates(action):
            old = conn.execute(
                text("SELECT payload, inputs FROM agent_actions WHERE id = :id"), {"id": action.id}
            ).one()
            generation = _generation(conn, ctx.campaign_id, "send_follow_up", ctx.step.step_key)
            board.propose(
                conn,
                ctx.step,
                ctx.run_id,
                action_type="send_follow_up",
                payload=old.payload,
                provenance=(old.inputs or {}).get("provenance", {}),
                step_id=f"{ctx.step.step_key}.{generation}",
            )
            return {"next": None}
        elif action.status in ("rejected", "cancelled"):
            conn.execute(
                text(
                    """UPDATE campaign_steps SET status = 'skipped', finished_at = now(),
                              waiting_for_approval_id = NULL,
                              error_code = :code WHERE id = :id"""
                ),
                {"id": ctx.step.id, "code": action.outcome_code or action.status},
            )
        else:
            board.fail(conn, ctx.step.id, action.outcome_code or action.status)
        _close(conn, ctx)
    return {"next": None}


def _applied_on(conn: Connection, campaign_id: UUID) -> str | None:
    application = apply.application_for_campaign(conn, campaign_id)
    if application is None:
        return None
    when = next((e.occurred_at for e in application.events if e.status == "applied"), None)
    return None if when is None else f"{when.day} {when:%B %Y}"


def _draft_follow_up(ctx: Ctx, c: Campaign, n: int) -> dict[str, Any]:
    with _tx(ctx.user_id) as conn:
        facts = _facts(conn, ctx.user_id)
        job = ingest.get_job(conn, ctx.job_id) or {}
        dossier_id, claims = _dossier(conn, ctx.campaign_id)
        applied_on = _applied_on(conn, ctx.campaign_id)
        skills, job_terms = _terms(conn, ctx.user_id, job)
        user = conn.execute(text("SELECT email, display_name FROM users WHERE id = :u"), {"u": ctx.user_id}).one()
    events = chaser.events_for(applied_on or "the date recorded in the tracker", str(job.get("title", "")))
    claim_texts = [str(m["text"]) for m in _model_view(claims)]
    requirements = [str(r["text"]) for r in job.get("requirements", [])]
    try:
        with llm.attributed(campaign_id=ctx.campaign_id, agent_run_id=ctx.run_id) as used:
            message = chaser.draft(facts, claim_texts, events, requirements, principal=ctx.user_id)
            kept, removed = chaser.validate(
                message,
                facts=facts,
                claims=claim_texts,
                events=events,
                skills=skills,
                job_terms=job_terms,
                principal=ctx.user_id,
            )
    except AIDisabled:
        return _fail(ctx, "ai_disabled", {"todo": "no_supported_draft"})
    except SchemaInvalid:
        return _fail(ctx, "no_supported_draft", {"todo": "no_supported_draft"})
    if not kept:
        with _tx(ctx.user_id) as conn:
            _model_row(conn, ctx, "Drafted a follow-up; nothing supported remained.", used.call_ids, "chaser.draft")
            board.fail(
                conn, ctx.step.id, "no_supported_draft", {"todo": "no_supported_draft", "removed_by_reason": removed}
            )
            _close(conn, ctx, status="failed", code="no_supported_draft")
        return {"next": None}
    name = (user.display_name or "").strip() or "Candidate"
    assert c.follow_up_email is not None
    payload = chaser.compose_payload(
        kept, to=c.follow_up_email, reply_to=user.email, job_title=str(job.get("title", "")), display_name=name
    )
    fact_alias = common.aliases("f", facts)
    cited = []
    for sentence in kept:
        cited.append(
            {
                "field": "body",
                "text": sentence.text.strip(),
                "fact_ids": [fact_alias[a].id for a in sentence.facts],
                "claim_indexes": [int(a[1:]) - 1 for a in sentence.dossier_claims],
                "events": [events[int(a[1:]) - 1] for a in sentence.events],
            }
        )
    provenance = {
        "fields": {"subject": "code", "body": "model"},
        "sentences": cited,
        "code_parts": ["Hello,", f"Best regards,\n{' '.join(name.split())}"],
        "dossier_id": None if dossier_id is None else str(dossier_id),
        "input_flags": [],
    }
    with _tx(ctx.user_id) as conn:
        _model_row(conn, ctx, "Drafted the follow-up.", used.call_ids, "chaser.draft")
        generation = _generation(conn, ctx.campaign_id, "send_follow_up", f"follow_up:{n}")
        board.propose(
            conn,
            ctx.step,
            ctx.run_id,
            action_type="send_follow_up",
            payload=payload,
            provenance=provenance,
            step_id=f"follow_up:{n}.{generation}",
        )
    return {"next": None}


# ---- interview_prep (the Coach) ----------


def interview_prep_node(state: "State") -> dict[str, Any]:
    ctx = _enter(state, "interview_prep", "coach")
    if ctx is None:
        return {"next": None}
    with _tx(ctx.user_id) as conn:
        facts = _facts(conn, ctx.user_id)
        job = ingest.get_job(conn, ctx.job_id) or {}
        _, claims = _dossier(conn, ctx.campaign_id)
        _, verdicts = _decision(conn, ctx.user_id, ctx.job_id)
        skills, job_terms = _terms(conn, ctx.user_id, job)
    requirements = _requirements(job)
    claim_texts = [str(m["text"]) for m in _model_view(claims)]
    try:
        with llm.attributed(campaign_id=ctx.campaign_id, agent_run_id=ctx.run_id) as used:
            pack = coach.draft(facts, requirements, verdicts, claim_texts, principal=ctx.user_id)
            kept, removed = coach.validate(
                pack,
                facts=facts,
                requirements=requirements,
                verdicts=verdicts,
                claims=claim_texts,
                skills=skills,
                job_terms=job_terms,
                principal=ctx.user_id,
            )
    except AIDisabled:
        return _fail(ctx, "ai_disabled")
    except SchemaInvalid:
        return _fail(ctx, "schema_invalid")
    output = coach.to_output(kept, facts=facts, requirements=requirements, removed_by_reason=removed)
    with _tx(ctx.user_id) as conn:
        _model_row(conn, ctx, "Prepared the interview questions.", used.call_ids, "coach.prep")
        board.complete(conn, ctx.step.id, output)
        _close(conn, ctx)
    return {"next": None}
