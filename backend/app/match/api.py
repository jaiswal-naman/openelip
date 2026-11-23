"""What other modules may call from app.match (the shared contracts in the Phase 4 and 5 indexes). Nothing else in this
package is a public interface.

  brief_matches(conn, user_id, local_date)   what a day's brief lists
  activity_since(conn, user_id, since)        the runs that finished after a time, for "done since the last brief"
  nightly_status(conn, user_id, local_date)   whether the day's nightly run is still to come, running, done or failed
  run(user_id, trigger=..., local_date=...)   one matching run for a person; returns a RunResult
  retrieve(conn, user_id, mode=...)           the candidates for a person's own record (keyword, vector or hybrid)
  violations(job, constraints)                the independent checker: the hard constraints a job breaks
  run_role(role_id, job_id, candidate_ids)    one employer role against pooled candidates: introduced or withheld
  role_selections(conn, run_id)               the candidates a role run introduced, with their evidence
  latest_role_run(conn, role_id)              the newest run for a role
  RANKER_VERSION                              the name of the ranking rules, stored on every run
  get_decision(conn, user_id, decision_id)    one decision of the person's, with its evidence (the chat agent's read)
  list_decisions(conn, user_id, ...)          the person's decisions by outcome and date, newest first
  decision_for_job(conn, user_id, job_id)     the newest decision for a job, else the on-request constraint check
  list_runs(conn, user_id, ...)               the person's own candidate runs, newest first, for the runs list
  agent_reads                                 shown decisions and their explanation for a candidate's agent token
  (09-04)

BriefMatch, RunActivity, NightlyStatus, RunResult and DecisionView are what they return.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from . import agent_reads, ledger
from .checker import violations
from .models import MATCH_OUTCOMES
from .queries import BriefMatch, NightlyStatus, RunActivity, activity_since, brief_matches, nightly_status
from .ranking import RANKER_VERSION
from .retrieval import retrieve
from .role_run import (
    RoleEvidence,
    RoleRunInfo,
    RoleRunResult,
    RoleSelection,
    latest_role_run,
    role_selections,
    run_role,
)
from .service import RunResult, run

__all__ = [
    "agent_reads",
    "RANKER_VERSION",
    "BriefMatch",
    "DecisionConstraint",
    "DecisionEvidence",
    "DecisionView",
    "RoleEvidence",
    "RoleRunInfo",
    "RoleRunResult",
    "RoleSelection",
    "NightlyStatus",
    "RunActivity",
    "RunResult",
    "activity_since",
    "latest_role_run",
    "brief_matches",
    "decision_for_job",
    "get_decision",
    "list_decisions",
    "nightly_status",
    "retrieve",
    "role_selections",
    "run",
    "run_role",
    "violations",
    "MatchRunRow",
    "list_runs",
]


@dataclass(frozen=True)
class DecisionEvidence:
    """One requirement of a scored job: the posting's own words, the verdict, and the facts the verdict cites."""

    requirement_id: int
    requirement: str
    importance: str  # required | preferred
    verdict: str  # met | partly_met | not_met | no_evidence
    fact_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class DecisionConstraint:
    """A hard constraint a job broke. The person may have removed it since: then there is no id and no sentence, and
    the ledger does not guess what it said."""

    kind: str
    constraint_id: UUID | None
    statement: str | None


@dataclass(frozen=True)
class DecisionView:
    """A decision as a reader sees it, by real ids. decision_id is None for an on-request check (nothing is stored).
    outcome is shown, withheld, filtered or, for an on-request check that found no violation, not_evaluated. evidence
    is filled by get_decision and decision_for_job, and empty in a list and for a job that was never scored."""

    decision_id: UUID | None
    job_id: int
    job_title: str
    company_name: str
    local_date: date
    outcome: str
    reason_codes: tuple[str, ...]
    violated_kinds: tuple[str, ...]
    constraints: tuple[DecisionConstraint, ...]  # one per violated kind
    unverified: tuple[str, ...]  # constrained fields the posting leaves unstated
    summary: str | None  # the line code rendered from the evidence, for a shown decision
    change_note: str | None  # what changed, for a job shown again
    evidence: tuple[DecisionEvidence, ...]
    is_on_request: bool


def _view(row: dict[str, Any], active: dict[str, dict[str, Any]]) -> DecisionView:
    """A ledger row as a view. `active` is the person's active constraints by kind (ledger.active_constraints)."""
    kinds = tuple(row.get("violated_kinds", ()))
    constraints = []
    for kind in kinds:
        found = active.get(kind)
        constraints.append(
            DecisionConstraint(
                kind=kind,
                constraint_id=UUID(str(found["id"])) if found else None,
                statement=found["statement"] if found else None,
            )
        )
    evidence = tuple(
        DecisionEvidence(
            requirement_id=item["requirement"]["id"],
            requirement=item["requirement"]["text"],
            importance=item["requirement"]["importance"],
            verdict=item["verdict"],
            fact_ids=tuple(UUID(str(cited["fact_id"])) for cited in item["record_citations"]),
        )
        for item in row.get("evidence", ())
    )
    job = row["job"]
    return DecisionView(
        decision_id=row["decision_id"],
        job_id=job["id"],
        job_title=job["title"],
        company_name=job["company_name"],
        local_date=row["local_date"],
        outcome=row["outcome"],
        reason_codes=tuple(row["reason_codes"]),
        violated_kinds=kinds,
        constraints=tuple(constraints),
        unverified=tuple(row.get("unverified", ())),
        summary=row.get("summary"),
        change_note=row.get("change_note"),
        evidence=evidence,
        is_on_request=row.get("evaluated") == "on_request",
    )


def _views(conn: Connection, user_id: UUID, rows: list[dict[str, Any]]) -> list[DecisionView]:
    active = ledger.active_constraints(conn, user_id) if any(row.get("violated_kinds") for row in rows) else {}
    return [_view(row, active) for row in rows]


def get_decision(conn: Connection, user_id: UUID, decision_id: UUID) -> DecisionView | None:
    """One decision of this person with its evidence table. None when it is not theirs or does not exist."""
    row = ledger.decision_by_id(conn, user_id, decision_id)
    return None if row is None else _views(conn, user_id, [row])[0]


def list_decisions(
    conn: Connection,
    user_id: UUID,
    *,
    outcome: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = 20,
) -> list[DecisionView]:
    """The person's decisions, newest local date first, at most `limit`, optionally of one outcome and between two local
    dates (both ends included). No evidence: get_decision has the table. An unknown outcome raises ValueError."""
    if outcome is not None and outcome not in MATCH_OUTCOMES:
        raise ValueError(f"outcome must be one of {MATCH_OUTCOMES}")
    rows, _ = ledger.decisions_page(
        conn, user_id, outcomes=[outcome] if outcome else None, since=since, until=until, limit=limit
    )
    return _views(conn, user_id, rows)


def decision_for_job(conn: Connection, user_id: UUID, job_id: int) -> DecisionView | None:
    """The newest stored decision for a job with its evidence. With none, the job is checked against the person's hard
    constraints on the spot (the SQL filter every run uses; no model, nothing stored): filtered with the kinds it broke,
    or not_evaluated. None for a job that is not in the index."""
    stored, _ = ledger.decisions_page(conn, user_id, job_id=job_id, limit=1)
    if stored:
        return get_decision(conn, user_id, stored[0]["decision_id"])
    today = ledger.today_in(ledger.timezone_of(conn, user_id))
    checked = ledger.check_job(conn, user_id, job_id, today)
    return None if checked is None else _views(conn, user_id, [checked])[0]


@dataclass(frozen=True)
class MatchRunRow:
    """One of the person's candidate matching runs. `status` is the runs list's word (`completed` reads `succeeded`)."""

    id: UUID
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None


# match_runs.status -> the word agent runs use
_RUN_STATUS = {"running": "running", "completed": "succeeded", "failed": "failed"}


def list_runs(
    conn: Connection,
    user_id: UUID,
    *,
    status: str | None = None,
    before: tuple[datetime, UUID] | None = None,
    limit: int = 50,
) -> list[MatchRunRow]:
    """The person's own candidate matching runs (an employer's role runs are not theirs), newest first, those that
    started before `before` (a `(started_at, id)` position). `status` uses the agent-run words: `running`, `succeeded`,
    `failed`; `cancelled` matches nothing. Runs on the caller's own connection: the API login sees only their rows."""
    stored = {v: k for k, v in _RUN_STATUS.items()}
    if status is not None and status not in stored:
        return []
    where = ["r.user_id = :u", "r.trigger <> 'role'"]
    params: dict[str, Any] = {"u": user_id, "limit": limit}
    if status is not None:
        where.append("r.status = CAST(:status AS match_run_status)")
        params["status"] = stored[status]
    if before is not None:
        where.append("(r.started_at, r.id) < (:at, :id)")
        params["at"], params["id"] = before
    rows = conn.execute(
        text(
            f"""SELECT r.id, r.trigger::text AS trigger, r.status::text AS status, r.started_at, r.finished_at
                FROM match_runs r WHERE {" AND ".join(where)}
                ORDER BY r.started_at DESC, r.id DESC LIMIT :limit"""
        ),
        params,
    ).all()
    return [MatchRunRow(r.id, r.trigger, _RUN_STATUS[r.status], r.started_at, r.finished_at) for r in rows]
