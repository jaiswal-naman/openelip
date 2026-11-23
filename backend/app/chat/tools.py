"""The chat agent's seven tools (plan 05-04; AI_SYSTEM 9; CHT-01 to CHT-04): six that read and one that proposes.

    get_record             the candidate's facts, constraints and preferences
    list_matches           their decisions, by outcome and date
    get_decision           one decision with its evidence, or the newest one for a job
    search_jobs            the job index, always under the candidate's own hard constraints (CHT-02)
    get_job                one posting: typed fields, requirements, the text
    get_applications       what they applied to and where it stands
    propose_record_change  record.propose on the current message; a proposal waits for the candidate (CHT-03)

None of them sends, fetches or approves anything, and none can change the record: the only write is a `proposed`
profile event, which changes nothing until the candidate confirms it in the app. Every tool is a function
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
from app.ingest import api as ingest
from app.match import api as match
from app.platform import db
from app.profile import api as profile

from . import proposals
from .refs import AliasTable
from .schemas import (
    MAX_SEARCH_RESULTS,
    GetApplications,
    GetDecision,
    GetJob,
    GetRecord,
    ListMatches,
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
}
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
    """The seven tools for one candidate message. `user_text` is that message, which is all record.propose reads."""
    return {
        "get_record": get_record,
        "list_matches": list_matches,
        "get_decision": get_decision,
        "search_jobs": search_jobs,
        "get_job": get_job,
        "get_applications": get_applications,
        "propose_record_change": _propose_record_change(user_text),
    }
