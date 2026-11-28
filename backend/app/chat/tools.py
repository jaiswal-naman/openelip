"""The chat agent's eleven tools (plans 05-04, 07-06; AI_SYSTEM 9; CHT-01 to CHT-04): nine read, two propose.

    get_record             the candidate's facts, constraints and preferences
    list_matches           their decisions, by outcome and date
    get_decision           one decision with its evidence, or the newest one for a job
    search_jobs            the job index, always under the candidate's own hard constraints (CHT-02)
    get_job                one posting: typed fields, requirements, the text
    get_applications       what they applied to and where it stands
    propose_record_change  record.propose on the current message; a proposal waits for the candidate (CHT-03)
    get_campaign           a job's campaign (stage, pause, next step, steps), or the open ones (plan 07-06)
    get_dossier            the company dossier of a job's campaign, each claim with its source and time
    explain_block          why work stopped or an action did not go ahead, in sentences the app wrote
    propose_campaign       offers a "Start campaign" card; the candidate's click starts it, this starts nothing

None of them sends, fetches or approves anything, and none can change the record or start work: the only write is a
`proposed` profile event, which changes nothing until the candidate confirms it in the app. The campaign tools read
through campaigns.api on the API login. Every tool is a function
(user_id, parsed input, alias table) -> ToolOutput. The person is the session's and arrives as the first argument;
no tool input has a field for one, so a model cannot ask for anyone else's data. Each tool reads through the `api.py`
of the module that owns the data, on its own app_api transaction, so row-level security is in force as well.

What the model reads is `data`, and it holds aliases only: every decision, job, fact and constraint a result mentions is
named d1, j1, f1 or c1 (refs.AliasTable). The real ids go in `refs` by kind, which the stream and the stored tool row
carry. Every result is untrusted (decision 26): each carries posting or resume text, and the loop wraps it as data.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.apply import api as applications
from app.campaigns import api as campaigns
from app.ingest import api as ingest
from app.match import api as match
from app.platform import db
from app.profile import api as profile

from . import proposals
from .refs import AliasTable
from .schemas import (
    MAX_SEARCH_RESULTS,
    CampaignProposal,
    ExplainBlock,
    GetApplications,
    GetCampaign,
    GetDecision,
    GetDossier,
    GetJob,
    GetRecord,
    ListMatches,
    ProposeCampaign,
    ProposeRecordChange,
    SearchJobs,
    ToolOutput,
)

SEARCH_LIMIT_MAX = (
    MAX_SEARCH_RESULTS  # assumption: results one search_jobs call returns; the step's schema holds the same
)
POSTING_CHARS = 6000  # assumption: characters of a posting the model reads (the whole text is in the index)
LIST_LIMIT = 20  # assumption: decisions or applications one call returns, newest first
FACTS_MAX = 100  # assumption: facts get_record returns; a longer record says how many were left out

Tool = Callable[[UUID, Any, AliasTable], ToolOutput]

TYPED_FIELDS = (
    "country_codes",
    "cities",
    "arrangements",
    "employment_types",
    "seniorities",
    "salary_floor",
    "company_names",
)
MISSING = {
    "unknown_reference": "No tool returned that reference in this conversation, so it cannot be looked up.",
    "not_found": "Nothing was found for that reference.",
    "no_campaign": "The candidate has no campaign for this job.",
    "no_dossier": "No company dossier exists yet for this job.",
}
CAMPAIGN_NOTE = (
    "This only offers a button. The candidate decides: nothing was started, and a campaign starts only when they "
    "click it in the app."
)
NO_BLOCKS = "Nothing is blocked or paused on this job's campaign."
PROPOSAL_NOTE = (
    "These are proposals only. Nothing in the record changes until the candidate confirms each one in the app."
)


class _Seen:
    """The things one result mentions. `alias` returns the alias the model will read and remembers the real id by kind,
    so the two cannot drift apart; `by_kind` is the result's refs (decision, job, fact, constraint), each id once."""

    def __init__(self, aliases: AliasTable) -> None:
        self._aliases = aliases
        self.by_kind: dict[str, list[str]] = {}

    def alias(self, kind: str, ref: str | int | UUID) -> str:
        ids = self.by_kind.setdefault(kind, [])
        if str(ref) not in ids:
            ids.append(str(ref))
        return self._aliases.add(kind, ref)


def _when(moment: datetime | None) -> str | None:
    """A time as UTC text, whatever zone the database session reads it in."""
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _added(moment: datetime) -> str:
    """The date a row was added, in UTC: the record keeps no separate 'last confirmed' time to show."""
    return moment.astimezone(timezone.utc).date().isoformat()


def _missing(reason: str) -> ToolOutput:
    return ToolOutput(data={"found": False, "reason": reason, "note": MISSING[reason]})


# ---- the record ----------------------------------------------------------------------------------------------------


def get_record(user_id: UUID, args: GetRecord, aliases: AliasTable) -> ToolOutput:
    with db.transaction(user_id) as conn:
        record = profile.get_record(conn, user_id)
    seen = _Seen(aliases)
    shown = record["facts"][:FACTS_MAX]
    listed = {fact["id"] for fact in shown}
    facts = []
    for fact in shown:
        row = {
            "fact": seen.alias("fact", fact["id"]),
            "kind": fact["kind"],
            "statement": fact["statement"],
            "source": fact["source"],
            "added": _added(fact["created_at"]),
        }
        if fact["attributes"]:
            row["attributes"] = fact["attributes"]
        if fact["parent_fact_id"] in listed:  # the role a bullet belongs to, when that role is shown too
            row["parent"] = seen.alias("fact", fact["parent_fact_id"])
        facts.append(row)
    constraints = [
        {
            "constraint": seen.alias("constraint", c["id"]),
            "kind": c["kind"],
            "statement": c["statement"],
            **{name: c[name] for name in TYPED_FIELDS if c.get(name)},
            "added": _added(c["created_at"]),
        }
        for c in record["constraints"]
    ]
    preferences = [
        {
            "kind": p["kind"],
            "value": p["value"],
            "weight": p["weight"],
            "statement": p["statement"],
            "notice": p["notice"],
            "added": _added(p["created_at"]),
        }
        for p in record["preferences"]
    ]
    data: dict[str, Any] = {
        "record_version": record["record_version"],
        "facts": facts,
        "constraints": constraints,
        "preferences": preferences,
        "waiting": {"unconfirmed_facts": record["unconfirmed_facts"], "open_proposals": record["open_proposals"]},
    }
    if len(record["facts"]) > FACTS_MAX:
        data["facts_omitted"] = len(record["facts"]) - FACTS_MAX
    return ToolOutput(data, seen.by_kind, source_kind="career_record")


# ---- decisions -----------------------------------------------------------------------------------------------------


def _decision(view: match.DecisionView, seen: _Seen, *, with_evidence: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "decision": None if view.decision_id is None else seen.alias("decision", view.decision_id),
        "job": seen.alias("job", view.job_id),
        "title": view.job_title,
        "company": view.company_name,
        "date": view.local_date.isoformat(),
        "outcome": view.outcome,
        "reasons": list(view.reason_codes),
        "violated_kinds": list(view.violated_kinds),
        "constraints": [
            {
                "constraint": None if c.constraint_id is None else seen.alias("constraint", c.constraint_id),
                "kind": c.kind,
                "statement": c.statement,
            }
            for c in view.constraints
        ],
        "unverified": list(view.unverified),
        "summary": view.summary,
        "change_note": view.change_note,
        "is_on_request": view.is_on_request,
    }
    if with_evidence:
        entry["evidence"] = [
            {
                "requirement": item.requirement,
                "importance": item.importance,
                "verdict": item.verdict,
                "facts": [seen.alias("fact", fact_id) for fact_id in item.fact_ids],
            }
            for item in view.evidence
        ]
    return entry


def list_matches(user_id: UUID, args: ListMatches, aliases: AliasTable) -> ToolOutput:
    with db.transaction(user_id) as conn:
        views = match.list_decisions(
            conn, user_id, outcome=args.outcome, since=args.since, until=args.until, limit=LIST_LIMIT
        )
    seen = _Seen(aliases)
    return ToolOutput({"decisions": [_decision(view, seen) for view in views]}, seen.by_kind, "decision_ledger")


def get_decision(user_id: UUID, args: GetDecision, aliases: AliasTable) -> ToolOutput:
    """By decision alias, or by job alias for the newest decision on that job (else the check the ledger runs on
    request). An alias that no tool returned in this session, or of the wrong kind, is answered without opening a
    connection."""
    alias, wanted = (args.decision, "decision") if args.decision is not None else (args.job or "", "job")
    found = aliases.get(alias)
    if found is None or found.kind != wanted:
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        if wanted == "decision":
            view = match.get_decision(conn, user_id, UUID(found.id))
        else:
            view = match.decision_for_job(conn, user_id, int(found.id))
    if view is None:  # not the caller's, or not in the index: the same answer
        return _missing("not_found")
    seen = _Seen(aliases)
    return ToolOutput(_decision(view, seen, with_evidence=True), seen.by_kind, "decision_ledger")


# ---- jobs ----------------------------------------------------------------------------------------------------------


def search_jobs(user_id: UUID, args: SearchJobs, aliases: AliasTable) -> ToolOutput:
    """The index's hybrid search with the caller's hard constraints inside the statement. `constrained_for` is the
    session's person, never a field of the input, and the search refuses a transaction that is not theirs."""
    with db.transaction(user_id) as conn:
        page = ingest.search(
            conn,
            q=args.q,
            country=args.country,
            city=args.city,
            remote=args.remote,
            limit=min(args.limit, SEARCH_LIMIT_MAX),
            cursor=None,
            mode="hybrid",
            constrained_for=user_id,
        )
    seen = _Seen(aliases)
    results = [
        {
            "job": seen.alias("job", row["id"]),
            "title": row["title"],
            "company": row["company_name"],
            "location": row["location_raw"],
            "country": row["country"],
            "city": row["city"],
            "work_arrangements": row["work_arrangements"],
            "employment_type": row["employment_type"],
            "pay": row["pay"],
        }
        for row in page.rows
    ]
    data = {"constraints_applied": True, "mode": page.mode, "results": results}
    return ToolOutput(data, seen.by_kind, "job_search")


def get_job(user_id: UUID, args: GetJob, aliases: AliasTable) -> ToolOutput:
    found = aliases.get(args.job)
    if found is None or found.kind != "job":
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        job = ingest.get_job(conn, int(found.id))
    if job is None:
        return _missing("not_found")
    seen = _Seen(aliases)
    posting = job["description"] or ""
    data = {
        "job": seen.alias("job", job["id"]),  # no link and no source board: nothing for the model to pass on as a URL
        "title": job["title"],
        "company": job["company_name"],
        "location": job["location_raw"],
        "country": job["country"],
        "city": job["city"],
        "work_arrangements": job["work_arrangements"],
        "is_remote": job["is_remote"],
        "employment_type": job["employment_type"],
        "pay": job["pay"],
        "status": job["status"],
        "posted_at": _when(job["posted_at"]),
        "closed_at": _when(job["closed_at"]),
        "requirements_known": job["requirements_version"] == job["content_version"],
        "requirements": [
            {"text": r["text"], "importance": r["importance"], "kind": r["kind"], "min_years": r["min_years"]}
            for r in job["requirements"]
        ],
        "posting_text": posting[:POSTING_CHARS],
        "posting_truncated": len(posting) > POSTING_CHARS,
    }
    return ToolOutput(data, seen.by_kind, "job_posting")


# ---- applications and proposals ------------------------------------------------------------------------------------


def get_applications(user_id: UUID, args: GetApplications, aliases: AliasTable) -> ToolOutput:
    with db.transaction(user_id) as conn:
        found = applications.list_applications(conn, user_id)
    seen = _Seen(aliases)
    rows = [
        {
            "job": seen.alias("job", application.job_id),
            "title": application.job_title,
            "company": application.company_name,
            "status": application.status,
            "status_changed_at": _when(application.status_changed_at),
            "history": [{"status": e.status, "occurred_at": _when(e.occurred_at)} for e in application.events],
        }
        for application in found[:LIST_LIMIT]
    ]
    data: dict[str, Any] = {"applications": rows}
    if len(found) > LIST_LIMIT:
        data["omitted"] = len(found) - LIST_LIMIT
    return ToolOutput(data, seen.by_kind, "applications")


# ---- campaigns (plan 07-06) ----------------------------------------------------------------------------------------


def _job_alias(aliases: AliasTable, alias: str | None) -> int | None:
    """The job number an alias of kind job stands for in this session, else None (unknown, or another kind)."""
    found = None if alias is None else aliases.get(alias)
    return int(found.id) if found is not None and found.kind == "job" else None


def _campaign(view: dict[str, Any], seen: _Seen, *, with_steps: bool) -> dict[str, Any]:
    step = view["next_step"]
    row: dict[str, Any] = {
        "job": seen.alias("job", view["job_id"]),
        "title": view["title"],
        "company": view["company_name"],
        "stage": view["stage"],
        "from_introduction": view["from_introduction"],
        "paused": view["paused_at"] is not None,
        "pause_reason": view["pause_reason"],
        "closed_reason": view["closed_reason"],
        "next_step": None if step is None else step["text"],
        "pending_approvals": view["pending_approvals"],
    }
    if with_steps:
        row["steps"] = [{"step": x["step_key"], "status": x["status"], "text": x["text"]} for x in view["steps"]]
    return row


def get_campaign(user_id: UUID, args: GetCampaign, aliases: AliasTable) -> ToolOutput:
    """One job's campaign (its open one, else its latest), or the open campaigns when no job is named."""
    seen = _Seen(aliases)
    if args.job is None:
        with db.transaction(user_id) as conn:
            open_ones = campaigns.open_campaigns(conn, user_id)
        return ToolOutput(
            {"campaigns": [_campaign(v, seen, with_steps=False) for v in open_ones]}, seen.by_kind, "campaigns"
        )
    job_id = _job_alias(aliases, args.job)
    if job_id is None:
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        view = campaigns.for_job(conn, user_id, job_id)
    if view is None:
        return _missing("no_campaign")
    return ToolOutput(_campaign(view, seen, with_steps=True), seen.by_kind, "campaign")


def get_dossier(user_id: UUID, args: GetDossier, aliases: AliasTable) -> ToolOutput:
    """The newest company dossier of the job's campaign: each claim with the page it came from and when it was read."""
    job_id = _job_alias(aliases, args.job)
    if job_id is None:
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        dossier = campaigns.dossier_for_job(conn, user_id, job_id)
    if dossier is None:
        return _missing("no_dossier")
    seen = _Seen(aliases)
    claims = [
        {
            "claim": c.get("text"),
            "topic": c.get("topic"),
            "sources": [
                {"url": src.get("url"), "retrieved_at": src.get("retrieved_at")} for src in c.get("sources", [])
            ],
        }
        for c in dossier["claims"]
    ]
    data = {
        "job": seen.alias("job", job_id),
        "found": True,
        "company": dossier["company_name"],
        "version": dossier["version"],
        "incomplete": dossier["incomplete"],
        "claims": claims,
        "not_found_on_company_pages": dossier["missing_topics"],
    }
    return ToolOutput(data, seen.by_kind, "company_dossier")


def explain_block(user_id: UUID, args: ExplainBlock, aliases: AliasTable) -> ToolOutput:
    """Why work stopped or an action did not go ahead: sentences the app wrote from reason codes, nothing else."""
    job_id = _job_alias(aliases, args.job)
    if job_id is None:
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        view = campaigns.for_job(conn, user_id, job_id)
        blocks = [] if view is None else campaigns.blocks_for_job(conn, user_id, job_id)
    if view is None:
        return _missing("no_campaign")
    seen = _Seen(aliases)
    data = {"job": seen.alias("job", job_id), "blocks": blocks, "note": None if blocks else NO_BLOCKS}
    return ToolOutput(data, seen.by_kind, "campaign_blocks")


def propose_campaign(user_id: UUID, args: ProposeCampaign, aliases: AliasTable) -> ToolOutput:
    """Offer to start a campaign for a job. Nothing is written: the stream gets a `campaign_proposal` event and the
    panel shows a card whose button calls POST /v1/campaigns, which has its own refusals (closed job, constraint)."""
    job_id = _job_alias(aliases, args.job)
    if job_id is None:
        return _missing("unknown_reference")
    with db.transaction(user_id) as conn:
        job = ingest.get_job(conn, job_id)
        open_id = campaigns.open_campaign_for_job(conn, user_id, job_id) if job is not None else None
    if job is None:
        return _missing("not_found")
    seen = _Seen(aliases)
    data = {
        "offered": True,
        "job": seen.alias("job", job_id),
        "title": job["title"],
        "company": job["company_name"],
        "campaign_already_open": open_id is not None,
        "note": CAMPAIGN_NOTE,
    }
    offer = CampaignProposal(job_id, job["title"], job["company_name"], open_id)
    return ToolOutput(data, seen.by_kind, "campaign_offer", campaign_proposals=(offer,))


def _propose_record_change(user_text: str) -> Tool:
    """record.propose over the candidate's current message and nothing else (CHT-03). The proposals are stored as
    `proposed` events by profile.api and come back as ProposalEvents for the stream; the record does not change."""

    def propose_record_change(user_id: UUID, args: ProposeRecordChange, aliases: AliasTable) -> ToolOutput:
        events = proposals.propose(user_id, user_text, principal=user_id)
        data = {
            "proposed": len(events),
            "proposals": [{"entity": e.entity, "operation": e.operation, "statement": e.statement} for e in events],
            "note": PROPOSAL_NOTE,
        }
        return ToolOutput(data, source_kind="record_proposals", proposals=tuple(events))

    return propose_record_change


def build_tools(user_text: str) -> dict[str, Tool]:
    """The eleven tools for one candidate message. `user_text` is that message, which is all record.propose reads."""
    return {
        "get_record": get_record,
        "list_matches": list_matches,
        "get_decision": get_decision,
        "search_jobs": search_jobs,
        "get_job": get_job,
        "get_applications": get_applications,
        "propose_record_change": _propose_record_change(user_text),
        "get_campaign": get_campaign,
        "get_dossier": get_dossier,
        "explain_block": explain_block,
        "propose_campaign": propose_campaign,
    }
