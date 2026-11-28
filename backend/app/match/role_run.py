"""The role run (ARCHITECTURE 4.5; HIR-03, HIR-04, HIR-07): one employer role against a list of candidate ids.

It is the candidate run of service.py turned around, and it reuses that module's pieces instead of copying them: the
same one-retry scoring (_answer), the same citation validator, the same decision and evidence inserts, the same
independent checker as the last look.

    1 prepare   the match role: the run row, the role's job and requirements, then per candidate the hard filter in SQL
                (a candidate the role breaks is counted, never stored, never sent to a model) and the usable facts
    2 score     no connection held: one request per candidate, validated, one retry on a rejected set
    3 write     the match role, one transaction under a per-role lock: one decision per candidate that passed the
                filter, `introduced` (reason selected_for_role) with one evidence row per requirement, or `withheld`

A candidate is introduced only when the verdict set passes the validator, is not weak_fit and every required
requirement is met. The run is attributed to no candidate (match_runs.user_id is NULL); each decision belongs to its
candidate and carries the candidate's own local date. Role decisions never reach the candidate's own views
(queries.NOT_A_ROLE_RUN).

Who may be asked at all (open to introductions, AI processing agreed, not already asked, not a member of the
organization) is decided by the caller: the match role reads no consents. With no model nobody is scored or introduced.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.platform import db, log
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid
from app.profile import api as profile

from . import ai_gate, checker, evidence, ranking, summary
from .retrieval import Candidate
from .service import SCORE_LIMIT, _answer, _checker_constraints, _Decision, _insert_decision, _refused, _write_evidence
from .validator import Accepted, EvidenceRow

PRACTICAL_SCORE_LIMIT = SCORE_LIMIT  # assumption (06-INDEX): 40 candidates scored per run


@dataclass(frozen=True)
class RoleEvidence:
    """One requirement of the role and what the candidate's record says about it."""

    requirement: str
    importance: str
    verdict: str
    fact_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class RoleSelection:
    """A candidate the run introduced: the decision, its one-sentence summary and one evidence row per requirement."""

    user_id: UUID
    decision_id: UUID
    summary: str
    rows: tuple[RoleEvidence, ...]


@dataclass(frozen=True)
class RoleRunResult:
    """What a role run did. `filtered` candidates broke a hard constraint (counted, not stored); `scored` is how many
    were sent to the model; `withheld` is every unfiltered candidate that was not introduced."""

    run_id: UUID
    selected: list[RoleSelection]
    withheld: int
    filtered: int
    scored: int


@dataclass(frozen=True)
class RoleRunInfo:
    run_id: UUID
    status: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class _Job:
    candidate: Candidate  # the role's posting as a Candidate, so the shared inserts and the summary can read it
    description: str
    requirements: list[dict[str, Any]]


@dataclass(frozen=True)
class _Person:
    user_id: UUID
    local_date: date
    facts: list[dict[str, Any]]


@dataclass(frozen=True)
class _Prepared:
    run_id: UUID
    job: _Job | None  # None when the job is gone or has no requirements to score against
    people: list[_Person]  # passed the hard filter, never scored for this role first
    filtered: int


# ---- 1: prepare ----------------------------------------------------------------------------------------------------


def _load_job(conn: Connection, job_id: int) -> _Job | None:
    row = conn.execute(
        text(
            """SELECT j.id AS job_id, j.title, j.company_name, j.country, j.city,
                      j.work_arrangements::text[] AS work_arrangements, j.employment_type::text AS employment_type,
                      j.seniority::text AS seniority, j.salary_min, j.salary_max, j.salary_currency,
                      j.visa_sponsorship::text AS visa_sponsorship, j.content_version, j.requirements_version,
                      j.materially_changed_at AS changed_at, j.change_note, j.description
                 FROM jobs j WHERE j.id = :job AND j.status = 'open'"""
        ),
        {"job": job_id},
    ).one_or_none()
    if row is None:
        return None
    facts = checker.JobFacts(
        company_name=row.company_name,
        country=row.country,
        city=row.city,
        work_arrangements=tuple(row.work_arrangements),
        employment_type=row.employment_type,
        seniority=row.seniority,
        salary_min=row.salary_min,
        salary_max=row.salary_max,
        salary_currency=row.salary_currency,
        visa_sponsorship=row.visa_sponsorship,
    )
    candidate = Candidate(
        job_id=row.job_id,
        title=row.title,
        facts=facts,
        content_version=row.content_version,
        requirements_version=row.requirements_version,
        changed_at=row.changed_at,
        change_note=row.change_note,
        unverified=(),
        violated_kinds=(),
        retrieval_rank=0,
        rrf_score=0.0,
    )
    requirements = [
        {
            "id": r.id,
            "ordinal": r.ordinal,
            "text": r.text,
            "importance": r.importance,
            "kind": r.kind,
            "span": {"start": r.span_start, "end": r.span_end},
            "min_years": r.min_years,
        }
        for r in conn.execute(
            text(
                """SELECT r.id, r.ordinal, r.text, r.importance::text AS importance, r.kind::text AS kind,
                          lower(r.span) AS span_start, upper(r.span) AS span_end, r.min_years
                     FROM job_requirements r
                    WHERE r.job_id = :job AND r.content_version = :version ORDER BY r.ordinal"""
            ),
            {"job": job_id, "version": row.content_version},
        )
    ]
    if not requirements or row.requirements_version != row.content_version:
        return None  # nothing to score against: the requirements are not extracted for this version of the text
    return _Job(candidate, row.description, requirements)


def _open_run(conn: Connection, role_id: UUID, now: datetime) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO match_runs (trigger, role_id, local_date, ranker_version)
               VALUES ('role', :role, :day, :ranker) RETURNING id"""
        ),
        {"role": role_id, "day": now.date(), "ranker": ranking.RANKER_VERSION},
    ).scalar_one()


def _order(conn: Connection, job_id: int, candidate_ids: list[UUID]) -> list[UUID]:
    """Candidates never scored for this role first, then the ones scored longest ago, then by id (so a run repeats)."""
    last = {
        row.user_id: row.last
        for row in conn.execute(
            text(
                """SELECT d.user_id, max(d.created_at) AS last FROM match_decisions d
                    WHERE d.job_id = :job AND d.user_id = ANY(:ids) AND NOT ('not_scored' = ANY (d.reason_codes))
                    GROUP BY d.user_id"""
            ),
            {"job": job_id, "ids": candidate_ids},
        )
    }
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(set(candidate_ids), key=lambda user: (last.get(user, floor), user))


def _prepare(role_id: UUID, job_id: int, candidate_ids: list[UUID], now: datetime) -> _Prepared:
    with db.transaction(role="match") as conn:
        run_id = _open_run(conn, role_id, now)
        job = _load_job(conn, job_id)
        people: list[_Person] = []
        filtered = 0
        for user in _order(conn, job_id, candidate_ids):
            row = conn.execute(
                text(
                    """SELECT u.timezone, constraint_violations(j, compile_constraints(u.id)) <> '{}' AS violates
                         FROM users u, jobs j WHERE u.id = :u AND j.id = :job"""
                ),
                {"u": user, "job": job_id},
            ).one_or_none()
            if row is None:
                continue  # no active account: nobody to ask
            if row.violates:
                filtered += 1  # HIR-07 and invariant 1: before any model call, and nothing is stored
                continue
            people.append(
                _Person(user, now.astimezone(ZoneInfo(row.timezone)).date(), profile.usable_facts(conn, user))
            )
    return _Prepared(run_id, job, people, filtered)


def _may_score() -> bool:
    """Whether the evidence route can be called (a fake provider, or a real model with a key). ai_gate reads
    eval_runs, which the match role cannot, so it is asked on a worker connection."""
    with db.transaction(role="worker") as conn:
        try:
            ai_gate.require_available(conn)
        except AIDisabled:
            return False
    return True


def _names(user_ids: list[UUID]) -> dict[UUID, str | None]:
    """Display names, removed from the facts sent to the model (AI_SYSTEM 2.9); the match role cannot read them."""
    with db.transaction(role="worker") as conn:
        rows = conn.execute(text("SELECT id, display_name FROM users WHERE id = ANY(:ids)"), {"ids": user_ids})
        return {row.id: row.display_name for row in rows}


# ---- 2: score ------------------------------------------------------------------------------------------------------


@dataclass
class _Scores:
    accepted: dict[UUID, Accepted]
    rejected: set[UUID]
    answered: bool = False


def _score(prepared: _Prepared, limit: int, may_score: bool) -> _Scores:
    scores = _Scores({}, set())
    job = prepared.job
    if not may_score or job is None:
        return scores
    names = _names([person.user_id for person in prepared.people])
    for person in prepared.people[: max(limit, 0)]:
        name = names.get(person.user_id)
        request = evidence.build_request(
            person.facts, job.requirements, description=job.description, names=(name,) if name else ()
        )
        try:
            outcome = _answer(request, person.user_id)  # scoring is attributed to the candidate, as in a candidate run
        except AIDisabled:
            break
        except SchemaInvalid:
            continue  # invalid twice: this candidate is withheld as not_scored
        scores.answered = True
        if isinstance(outcome, Accepted):
            scores.accepted[person.user_id] = outcome
        else:
            scores.rejected.add(person.user_id)
    return scores


# ---- 3: decide and write ---------------------------------------------------------------------------------------------


def _decision(job: _Job, outcome: str, reason: str, rows: tuple[EvidenceRow, ...] = ()) -> _Decision:
    """The shared decision record: the role's posting, the outcome and reason, and for an introduced candidate the one
    sentence the rules write from the evidence."""
    text_of_summary = summary.render(rows, job.candidate.facts) if outcome == "introduced" else None
    return _Decision(job.candidate, outcome, (reason,), rows=rows, summary=text_of_summary)


def _judge(person: _Person, scores: _Scores, job: _Job) -> _Decision:
    accepted = scores.accepted.get(person.user_id)
    if accepted is None:
        return _decision(job, "withheld", "citation_unresolved" if person.user_id in scores.rejected else "not_scored")
    if accepted.weak_fit:
        return _decision(job, "withheld", "weak_fit", accepted.rows)
    if any(row.importance == "required" and row.verdict != "met" for row in accepted.rows):
        return _decision(job, "withheld", "required_not_met", accepted.rows)
    return _decision(job, "introduced", "selected_for_role", accepted.rows)


def _insert_scored(conn: Connection, run_id: UUID, person: _Person, d: _Decision) -> UUID | None:
    """A scored decision and its evidence in a savepoint, the completeness check run at once for an introduced one. None
    when the database refuses the evidence (a cited fact was superseded since scoring): nothing of it is left."""
    try:
        with conn.begin_nested():
            decision_id = _insert_decision(conn, run_id, person.user_id, person.local_date, d, None)
            _write_evidence(conn, decision_id, person.user_id, d.rows)
            if d.outcome == "introduced":
                conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
                conn.execute(text("SET CONSTRAINTS match_decisions_complete DEFERRED"))
    except DBAPIError as error:
        if _refused(error):
            return None
        raise
    return decision_id


def _write(prepared: _Prepared, scores: _Scores) -> tuple[list[RoleSelection], int, int]:
    """Returns (selected, withheld, filtered_at_write). The independent checker reads each person's constraints again
    here, so one added while the run scored still holds."""
    job = prepared.job
    selected: list[RoleSelection] = []
    withheld = filed = 0
    people = prepared.people if job is not None else []  # no job to judge against: nobody is decided, the run ends
    with db.transaction(role="match") as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"match.role_run:{prepared.run_id}"},
        )
        for person in people:
            assert job is not None
            constraints = _checker_constraints(profile.compiled_constraints(conn, person.user_id))
            if checker.violations(job.candidate.facts, constraints):
                filed += 1
                continue
            decision = _judge(person, scores, job)
            decision_id = _insert_scored(conn, prepared.run_id, person, decision) if decision.rows else None
            if decision.rows and decision_id is None:  # evidence refused: written again, withheld, with none
                decision = _decision(job, "withheld", "citation_unresolved")
            if decision_id is None:
                decision_id = _insert_decision(conn, prepared.run_id, person.user_id, person.local_date, decision, None)
            if decision.outcome == "introduced":
                selected.append(_selection(person.user_id, decision_id, decision))
            else:
                withheld += 1
        conn.execute(
            text(
                """UPDATE match_runs SET status = 'completed', finished_at = now(), retrieved_count = :retrieved,
                          filtered_count = :filtered, evidence_prompt_version = :prompt
                    WHERE id = :r"""
            ),
            {
                "r": prepared.run_id,
                "retrieved": len(prepared.people) - filed,
                "filtered": prepared.filtered + filed,
                "prompt": evidence.PROMPT_VERSION if scores.answered else None,
            },
        )
    return selected, withheld, filed


def _selection(user_id: UUID, decision_id: UUID, decision: _Decision) -> RoleSelection:
    rows = tuple(
        RoleEvidence(r.text, r.importance, r.verdict, tuple(UUID(f) for f in r.fact_ids)) for r in decision.rows
    )
    return RoleSelection(user_id, decision_id, decision.summary or "", rows)


def _fail(run_id: UUID, code: str) -> None:
    with db.transaction(role="match") as conn:
        conn.execute(
            text(
                """UPDATE match_runs SET status = 'failed', finished_at = now(), error_code = :c
                    WHERE id = :r AND status = 'running'"""
            ),
            {"c": code, "r": run_id},
        )


def run_role(
    role_id: UUID, job_id: int, candidate_ids: list[UUID], *, score_limit: int = PRACTICAL_SCORE_LIMIT
) -> RoleRunResult:
    """Run an open role's job against these candidates (already pooled: consent, not asked, not a member). Raises
    ProviderUnavailable when the model provider is down (the run is marked failed, and the caller retries)."""
    started, now = time.monotonic(), datetime.now(timezone.utc)
    prepared = _prepare(role_id, job_id, candidate_ids, now)
    try:
        scores = _score(prepared, score_limit, _may_score())
        selected, withheld, filed = _write(prepared, scores)
    except ProviderUnavailable:
        _fail(prepared.run_id, "provider_unavailable")
        raise
    except Exception:
        _fail(prepared.run_id, "internal")
        raise
    scored = len(scores.accepted) + len(scores.rejected)
    log.event(
        "match.role_run",
        level=logging.INFO,
        run_id=prepared.run_id,
        retrieved=len(prepared.people),
        scored=scored,
        introduced=len(selected),
        withheld=withheld,
        filtered=prepared.filtered + filed,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return RoleRunResult(prepared.run_id, selected, withheld, prepared.filtered + filed, scored)


# ---- reads for the hiring module -------------------------------------------------------------------------------------


def role_selections(conn: Connection, run_id: UUID) -> list[RoleSelection]:
    """The candidates a role run introduced, with their evidence, as the database holds them (oldest decision first)."""
    decisions = conn.execute(
        text(
            """SELECT d.id, d.user_id, d.summary FROM match_decisions d
                WHERE d.match_run_id = :run AND d.outcome = 'introduced' ORDER BY d.created_at, d.id"""
        ),
        {"run": run_id},
    ).all()
    if not decisions:
        return []
    found: dict[UUID, list[RoleEvidence]] = {d.id: [] for d in decisions}
    for row in conn.execute(
        text(
            """SELECT e.match_decision_id, r.text, r.importance::text AS importance, e.verdict::text AS verdict,
                      e.fact_ids
                 FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
                WHERE e.match_decision_id = ANY(:ids) ORDER BY e.match_decision_id, r.ordinal"""
        ),
        {"ids": [d.id for d in decisions]},
    ):
        found[row.match_decision_id].append(
            RoleEvidence(row.text, row.importance, row.verdict, tuple(UUID(str(f)) for f in row.fact_ids))
        )
    return [RoleSelection(d.user_id, d.id, d.summary, tuple(found[d.id])) for d in decisions]


def latest_role_run(conn: Connection, role_id: UUID) -> RoleRunInfo | None:
    """The newest run for the role, whatever its state (None before the first)."""
    row = conn.execute(
        text(
            """SELECT id, status::text AS status, started_at, finished_at FROM match_runs
                WHERE role_id = :role AND trigger = 'role' ORDER BY started_at DESC, id DESC LIMIT 1"""
        ),
        {"role": role_id},
    ).one_or_none()
    return None if row is None else RoleRunInfo(row.id, row.status, row.started_at, row.finished_at)
