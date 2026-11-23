"""The match run (ARCHITECTURE 4.3; MAT-01 to MAT-03, MAT-05 to MAT-07): one candidate, one pass over the job index.

    1 account   the worker connection: whether the person is active and agreed to AI processing (the match role may
                read neither consents nor names)
    2 prepare   the match role: the run row, the record, retrieval with the filter first, the filtered sample, pre-rank,
                and the requirements of the jobs to score
    3 score     no connection held: one request per job, validated, one retry on a rejected set
    4 write     the match role, one transaction under a per-person lock: the checker's second look, the ranking under
                the day's cap, and one decision per job with its evidence, each scored decision in a savepoint

Steps 2 and 4 are separate transactions because step 3 can take minutes, and a model call must not hold a connection.
The database is the last guard: the daily cap, shown-once, the citation trigger and the completeness trigger all hold
whatever this code does, and a refusal of one job withholds that job alone.

A person who has not agreed to AI processing, or an instance with no model, still gets the retrieval and the rules
ranking: every retrieved job is recorded withheld as not_scored and nothing is shown (AI_SYSTEM 2.10).
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.platform import db, log
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid
from app.profile import api as profile
from app.trust import api as trust

from . import checker, evidence, ranking, retrieval, summary
from .retrieval import Candidate
from .validator import Accepted, EvidenceRow, Rejected, validate

SCORE_LIMIT = 40  # assumption (ARCHITECTURE 5.8): pairs scored per run, which is what bounds a run's spend
DAILY_CAP = 5  # MAT-02: at most five shown per person and local date, across every run of the day
TRIGGERS = ("nightly", "onboarding", "on_demand")
CHECKER_DISAGREEMENT = "checker_disagreement"
CHANGED = "The posting changed."  # the note of a version that ingest wrote no note for
REFUSALS = ("citation_unresolved", "evidence_incomplete")  # what the database says when it refuses evidence
# assumption: the seniority ladder, lowest first, as the Career Record orders the values; a step is one rung
SENIORITY_ORDER = ("intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive")
SECONDS_PER_DAY = 86_400


class NotRunnable(Exception):
    """The person has no active account, so there is nothing to match for them."""


@dataclass(frozen=True)
class RunResult:
    """What a run did. `filtered` counts every job the constraints removed, stored or not; `checker` is "disagreed"
    when the independent checker found a violator the SQL filter let through (the job is filed as filtered)."""

    run_id: UUID
    shown: int
    withheld: int
    filtered: int
    checker: Literal["agreed", "disagreed"]


@dataclass(frozen=True)
class _ToScore:
    job: Candidate
    requirements: list[dict[str, Any]]  # ingest.api.get_job's requirement rows, at the job's current content version
    description: str  # the text the requirements' spans point into


@dataclass(frozen=True)
class _Prepared:
    run_id: UUID
    record_version: int
    facts: list[dict[str, Any]]
    preferences: list[dict[str, Any]]
    retrieved: list[Candidate]  # the eligible jobs, best retrieval first
    sample: list[Candidate]  # the best-ranked violators, stored so "why not" has rows
    filtered_total: int
    to_score: list[_ToScore]  # best pre-rank first


@dataclass
class _Scores:
    accepted: dict[int, Accepted]  # job id -> a verdict set that passed the validator
    rejected: set[int]  # job ids whose verdict sets failed it twice
    answered: bool = False  # a model answered at least once, so the run names the prompt it used


@dataclass(frozen=True)
class _Decision:
    job: Candidate
    outcome: str  # shown | withheld | filtered
    reasons: tuple[str, ...]
    violated: tuple[str, ...] = ()
    rows: tuple[EvidenceRow, ...] = ()  # the verdicts to store with it; none for a job that was not scored
    summary: str | None = None
    change_note: str | None = None


@dataclass(frozen=True)
class _Day:
    decisions: list[_Decision]  # shown first, in rank order
    free_positions: list[int]  # the day's rank positions no earlier run used
    filed: int  # jobs the checker filed as filtered
    disagreed: bool


# ---- soft signals: what the pre-rank and the final score read besides the verdicts --------------------------------


def _settled(preference: dict[str, Any], job: Candidate) -> bool | None:
    """Whether the posting has what the preference names: True or False when it says, None when it cannot tell. Only
    what a posting's own fields can settle counts; industry, company size, pay targets and the like count for nothing
    here (nothing is guessed from the text)."""
    kind, value, facts = preference["kind"], preference["value"].lower().strip(), job.facts
    if kind == "target_title":
        return True if all(word in job.title.lower() for word in value.split()) else None
    if kind == "remote_policy":
        return value in facts.work_arrangements if facts.work_arrangements else None
    if kind == "seniority":
        return facts.seniority == value if facts.seniority != "unknown" else None
    if kind == "location":
        stated = [place.lower() for place in (facts.city, facts.country) if place]
        return value in stated if stated else None
    return None


def _preference_match(job: Candidate, preferences: list[dict[str, Any]]) -> float:
    """-1 to 1: the share of the person's checkable soft preferences the posting meets, minus the share it breaks.
    A wanted seniority is measured by _seniority_distance, not counted twice."""
    counted = [
        p
        for p in preferences
        if p["kind"] in ("target_title", "remote_policy", "location") or (p["kind"] == "seniority" and p["weight"] < 0)
    ]
    verdicts = [(p["weight"] > 0, _settled(p, job)) for p in counted]
    met = sum(settled is not None and settled == wanted for wanted, settled in verdicts)
    broken = sum(settled is not None and settled != wanted for wanted, settled in verdicts)
    return (met - broken) / len(counted) if counted else 0.0


def _seniority_distance(job: Candidate, preferences: list[dict[str, Any]]) -> float:
    """Rungs between the posting's level and the nearest level the person wants; 0 when either is not stated."""
    wanted = [
        SENIORITY_ORDER.index(p["value"].lower().strip())
        for p in preferences
        if p["kind"] == "seniority" and p["weight"] > 0 and p["value"].lower().strip() in SENIORITY_ORDER
    ]
    if not wanted or job.facts.seniority not in SENIORITY_ORDER:
        return 0.0
    here = SENIORITY_ORDER.index(job.facts.seniority)
    return float(min(abs(here - rung) for rung in wanted))


def _recency(job: Candidate, now: datetime) -> float:
    return ranking.age_bucket((now - job.changed_at).total_seconds() / SECONDS_PER_DAY)


def _final_score(
    job: Candidate, rows: tuple[EvidenceRow, ...], preferences: list[dict[str, Any]], now: datetime
) -> float:
    """The rules score of a job that has verdicts: fit, plus the same soft signals as the pre-rank."""
    return ranking.final_score(
        ranking.fit_score(rows),
        _preference_match(job, preferences),
        _seniority_distance(job, preferences),
        _recency(job, now),
    )


def _prerank(found: list[Candidate], preferences: list[dict[str, Any]], now: datetime) -> list[Candidate]:
    """The retrieved jobs best first by the rules alone (no verdicts exist yet): the fusion score as a share of the
    best one, plus the soft signals. A tie goes to the lower job id, so a run repeats."""
    best = max((job.rrf_score for job in found), default=0.0) or 1.0

    def key(job: Candidate) -> tuple[float, int]:
        score = ranking.prerank_score(
            job.rrf_score / best,
            _preference_match(job, preferences),
            _seniority_distance(job, preferences),
            _recency(job, now),
        )
        return -score, job.job_id

    return sorted(found, key=key)


def _checker_constraints(compiled: dict[str, dict[str, Any]]) -> checker.Constraints:
    """The person's hard constraints as the checker's dataclass, read through profile.api and not through the SQL
    function compile_constraints, so the two locks do not share a compiler."""
    location, floor = compiled.get("location", {}), compiled.get("salary_floor", {}).get("salary_floor")
    names = compiled.get("excluded_company", {}).get("company_names")
    return checker.Constraints(
        countries=location.get("country_codes"),
        cities=location.get("cities"),
        arrangements=compiled.get("remote_policy", {}).get("arrangements"),
        authorized_countries=compiled.get("work_authorization", {}).get("country_codes"),
        salary_floor=floor["amount"] if floor else None,
        salary_currency=floor["currency"] if floor else None,
        employment_types=compiled.get("employment_type", {}).get("employment_types"),
        seniorities=compiled.get("seniority", {}).get("seniorities"),
        excluded_company_keys=None if names is None else [checker.company_key(name) for name in names],
    )


# ---- 1 and 2: who, and what to score ---------------------------------------------------------------------------------


def _account(user_id: UUID) -> tuple[str, str | None, bool]:
    """(time zone, display name, agreed to AI processing), read on the worker connection."""
    with db.transaction(user_id=user_id, role="worker") as conn:
        row = conn.execute(
            text("SELECT timezone, display_name FROM users WHERE id = :u AND status = 'active'"), {"u": user_id}
        ).one_or_none()
        if row is None:
            raise NotRunnable("no active account")
        try:
            trust.require_consent(conn, user_id, "ai_processing")
            agreed = True
        except trust.ConsentRequired:
            agreed = False
    return row.timezone, row.display_name, agreed


def _result(conn: Connection, run_id: UUID) -> RunResult:
    row = conn.execute(
        text(
            """SELECT r.shown_count, r.filtered_count, r.error_code,
                      (SELECT count(*) FROM match_decisions d WHERE d.match_run_id = r.id AND d.outcome = 'withheld')
                        AS withheld
                 FROM match_runs r WHERE r.id = :r"""
        ),
        {"r": run_id},
    ).one()
    if row.error_code == CHECKER_DISAGREEMENT:
        return RunResult(run_id, row.shown_count, row.withheld, row.filtered_count, "disagreed")
    return RunResult(run_id, row.shown_count, row.withheld, row.filtered_count, "agreed")


def _open_run(conn: Connection, user_id: UUID, trigger: str, day: date, version: int) -> tuple[UUID, bool]:
    """(run id, already finished). A nightly run is unique per person and date: a completed one is handed back, a failed
    or abandoned one is set back to running and done again. Any other trigger always makes a new row."""
    values = {"trigger": trigger, "u": user_id, "day": day, "version": version, "ranker": ranking.RANKER_VERSION}
    insert = """INSERT INTO match_runs (trigger, user_id, local_date, record_version, ranker_version)
                VALUES (CAST(:trigger AS match_trigger), :u, :day, :version, :ranker)"""
    if trigger != "nightly":
        return conn.execute(text(insert + " RETURNING id"), values).scalar_one(), False
    row = conn.execute(
        text(
            insert
            + """ ON CONFLICT (user_id, local_date) WHERE trigger = 'nightly' DO UPDATE
                    SET status = 'running', record_version = EXCLUDED.record_version, error_code = NULL,
                        started_at = now(), finished_at = NULL
                  WHERE match_runs.status <> 'completed'
                  RETURNING id"""
        ),
        values,
    ).one_or_none()
    if row is not None:
        return row.id, False
    done = conn.execute(
        text("SELECT id FROM match_runs WHERE user_id = :u AND local_date = :day AND trigger = 'nightly'"), values
    ).scalar_one()
    return done, True


def _requirement_rows(conn: Connection, ranked: list[Candidate]) -> dict[int, list[dict[str, Any]]]:
    """Each job's extracted requirements at the version the retrieval saw it at; a job whose requirements are not
    extracted for that version (or changed meanwhile) has none."""
    version = {job.job_id: job.content_version for job in ranked}
    rows = conn.execute(
        text(
            """SELECT r.job_id, r.content_version, r.id, r.ordinal, r.text, r.importance::text AS importance,
                      r.kind::text AS kind, lower(r.span) AS span_start, upper(r.span) AS span_end, r.min_years
                 FROM job_requirements r JOIN jobs j ON j.id = r.job_id AND j.requirements_version = r.content_version
                WHERE r.job_id = ANY(:ids) ORDER BY r.job_id, r.ordinal"""
        ),
        {"ids": list(version)},
    )
    found: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if row.content_version == version[row.job_id]:
            found.setdefault(row.job_id, []).append(
                {
                    "id": row.id,
                    "ordinal": row.ordinal,
                    "text": row.text,
                    "importance": row.importance,
                    "kind": row.kind,
                    "span": {"start": row.span_start, "end": row.span_end},
                    "min_years": row.min_years,
                }
            )
    return found


def _to_score(conn: Connection, ranked: list[Candidate], limit: int) -> list[_ToScore]:
    """The best `limit` jobs by pre-rank that have requirements to score against, with the posting text their spans
    point into. (A job with none, such as one nobody has extracted yet, would only use up a slot.)"""
    requirements = _requirement_rows(conn, ranked)
    chosen = [job for job in ranked if job.job_id in requirements][: max(limit, 0)]
    texts = {
        row.id: row
        for row in conn.execute(
            text("SELECT id, content_version, description FROM jobs WHERE id = ANY(:ids)"),
            {"ids": [job.job_id for job in chosen]},
        )
    }
    return [
        _ToScore(job, requirements[job.job_id], texts[job.job_id].description)
        for job in chosen
        if job.job_id in texts and texts[job.job_id].content_version == job.content_version
    ]


def _prepare(user_id: UUID, trigger: str, day: date, limit: int, now: datetime) -> _Prepared | RunResult:
    with db.transaction(user_id=user_id, role="match") as conn:
        version = conn.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": user_id}).scalar()
        if version is None:
            raise NotRunnable("no active account")
        run_id, finished = _open_run(conn, user_id, trigger, day, version)
        if finished:
            return _result(conn, run_id)
        facts = profile.usable_facts(conn, user_id)
        preferences = retrieval.active_preferences(conn, user_id)
        terms = retrieval.query_terms(facts, preferences)
        vector = retrieval.profile_vector(facts, preferences)
        found = retrieval.candidates(conn, user_id, vector=vector, terms=terms)
        sample = retrieval.filtered_sample(conn, user_id, vector=vector, terms=terms)
        total = retrieval.filtered_count(conn, user_id)
        scorable = _to_score(conn, _prerank(found, preferences, now), limit)
    return _Prepared(run_id, version, facts, preferences, found, sample, total, scorable)


# ---- 3: score ------------------------------------------------------------------------------------------------------


def _answer(request: evidence.EvidenceRequest, user_id: UUID) -> Accepted | Rejected:
    """A verdict set that passed the validator, or the second rejection: a rejected set gets one fresh call (AI_SYSTEM
    4.4). ProviderUnavailable, AIDisabled and SchemaInvalid are the caller's."""
    for _ in range(2):
        result = validate(evidence.score(request, principal=user_id), request, career_months=request.career_months)
        if isinstance(result, Accepted):
            return result
    return result


def _score(
    user_id: UUID, to_score: list[_ToScore], facts: list[dict[str, Any]], names: tuple[str, ...], agreed: bool
) -> _Scores:
    scores = _Scores({}, set())
    if not agreed:
        return scores  # no model sees a person's facts before they agree (TRU-05)
    for item in to_score:
        request = evidence.build_request(facts, item.requirements, description=item.description, names=names)
        try:
            outcome = _answer(request, user_id)
        except AIDisabled:
            break  # nothing is configured, nothing was sent, and no later job can be scored either
        except SchemaInvalid:
            continue  # invalid twice: this job is withheld as not_scored
        scores.answered = True
        if isinstance(outcome, Accepted):
            scores.accepted[item.job.job_id] = outcome
        else:
            scores.rejected.add(item.job.job_id)
    return scores


# ---- 4: decide and write -------------------------------------------------------------------------------------------


def _unfit(job: Candidate, state: Any, last_shown: Any) -> str | None:
    """Why a job cannot be shown now although it was eligible when retrieved: it closed, its text changed since it was
    read (its verdicts would be about other words), or a run that finished first showed it."""
    if state is None or state.status != "open":
        return "job_closed"
    if state.content_version != job.content_version:
        return "not_scored"
    if last_shown is not None and last_shown.job_content_version == job.content_version:
        return "already_shown"
    return None


def _shown_reasons(job: Candidate, last_shown: Any) -> tuple[tuple[str, ...], str | None]:
    """top_ranked, and when the person was shown an older version of this posting, material_change with the note ingest
    wrote (MAT-07): with several versions passed it says how many and when the person last saw it."""
    if last_shown is None:
        return ("top_ranked",), None
    passed = job.content_version - last_shown.job_content_version
    note = job.change_note or CHANGED
    if passed > 1:
        note = f"Changed {passed} times since you saw it on {last_shown.local_date.isoformat()}: {note}"
    return ("top_ranked", "material_change"), note


def _decide(conn: Connection, user_id: UUID, day: date, prepared: _Prepared, scores: _Scores, now: datetime) -> _Day:
    """Every decision of the run. The constraints are read afresh, so a constraint added while the run was scoring
    still holds: the checker is the last look before anything is shown."""
    constraints = _checker_constraints(profile.compiled_constraints(conn, user_id))
    record_now = conn.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": user_id}).scalar()
    ids = [job.job_id for job in prepared.retrieved]
    state = {
        r.id: r
        for r in conn.execute(
            text("SELECT id, status::text AS status, content_version FROM jobs WHERE id = ANY(:ids)"), {"ids": ids}
        )
    }
    last_shown = {
        r.job_id: r
        for r in conn.execute(
            text(
                """SELECT DISTINCT ON (job_id) job_id, job_content_version, local_date FROM match_decisions
                    WHERE user_id = :u AND outcome = 'shown' AND job_id = ANY(:ids)
                    ORDER BY job_id, job_content_version DESC"""
            ),
            {"u": user_id, "ids": ids},
        )
    }
    used = set(
        conn.execute(
            text(
                """SELECT rank_position FROM match_decisions
                    WHERE user_id = :u AND local_date = :day AND outcome = 'shown'"""
            ),
            {"u": user_id, "day": day},
        ).scalars()
    )
    others: list[_Decision] = []
    scored: dict[int, ranking.Scored] = {}
    filed, disagreed = 0, False
    for job in prepared.retrieved:
        if kinds := checker.violations(job.facts, constraints):
            filed += 1
            if record_now == prepared.record_version:  # the filter and the checker read the same record: they disagree
                disagreed = True
                log.event("match.checker_disagreement", level=logging.ERROR, job_id=job.job_id, violations=len(kinds))
            else:  # a constraint was added after retrieval: the checker is right and nothing is wrong
                log.event("match.constraint_added_during_run", job_id=job.job_id)
            others.append(_Decision(job, "filtered", ("hard_constraint",), tuple(kinds)))
        elif reason := _unfit(job, state.get(job.job_id), last_shown.get(job.job_id)):
            others.append(_Decision(job, "withheld", (reason,)))
        elif (accepted := scores.accepted.get(job.job_id)) is not None:
            final = _final_score(job, accepted.rows, prepared.preferences, now)
            scored[job.job_id] = ranking.Scored(
                job.job_id, job.facts.company_name, job.title, final, accepted.rows, accepted.weak_fit
            )
        else:
            others.append(
                _Decision(job, "withheld", ("citation_unresolved" if job.job_id in scores.rejected else "not_scored",))
            )
    free = [position for position in range(1, DAILY_CAP + 1) if position not in used]
    picked = ranking.pick(scored.values(), len(free))
    by_id = {job.job_id: job for job in prepared.retrieved}
    shown = []
    for pick in picked.shown:
        reasons, note = _shown_reasons(by_id[pick.job_id], last_shown.get(pick.job_id))
        job = by_id[pick.job_id]
        shown.append(
            _Decision(
                job, "shown", reasons, rows=pick.rows, summary=summary.render(pick.rows, job.facts), change_note=note
            )
        )
    held = [
        _Decision(by_id[job_id], "withheld", (reason,), rows=scored[job_id].rows)
        for job_id, reason in picked.held.items()
    ]
    taken = set(by_id)
    stored = [
        _Decision(job, "filtered", ("hard_constraint",), job.violated_kinds)
        for job in prepared.sample
        if job.job_id not in taken
    ]
    return _Day([*shown, *held, *others, *stored], free, filed, disagreed)


def _insert_decision(
    conn: Connection, run_id: UUID, user_id: UUID, day: date, d: _Decision, position: int | None
) -> UUID:
    job = d.job
    fit = ranking.fit_score(d.rows) if d.rows else None
    return conn.execute(
        text(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, violated_kinds, unverified, retrieval_rank, rrf_score,
                                            fit_score, rank_position, summary, change_note)
               VALUES (:run, :u, :job, :version, :day, CAST(:outcome AS match_outcome),
                       CAST(:reasons AS match_reason[]), CAST(:violated AS constraint_kind[]),
                       CAST(:unverified AS unverified_flag[]), :rank, :rrf, :fit, :position, :summary, :note)
               RETURNING id"""
        ),
        {
            "run": run_id,
            "u": user_id,
            "job": job.job_id,
            "version": job.content_version,
            "day": day,
            "outcome": d.outcome,
            "reasons": list(d.reasons),
            "violated": list(d.violated),
            "unverified": [] if d.outcome == "filtered" else list(job.unverified),
            "rank": job.retrieval_rank,
            "rrf": job.rrf_score,
            "fit": fit,
            "position": position,
            "summary": d.summary,
            "note": d.change_note,
        },
    ).scalar_one()


def _write_evidence(conn: Connection, decision_id: UUID, user_id: UUID, rows: tuple[EvidenceRow, ...]) -> None:
    """One evidence row per verdict. The citation trigger checks both sides of every row as it is inserted."""
    conn.execute(
        text(
            """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids)
               VALUES (:decision, :u, :requirement, CAST(:verdict AS evidence_verdict), CAST(:facts AS uuid[]))"""
        ),
        [
            {
                "decision": decision_id,
                "u": user_id,
                "requirement": r.requirement_id,
                "verdict": r.verdict,
                "facts": list(r.fact_ids),
            }
            for r in rows
        ],
    )


def _refused(error: DBAPIError) -> bool:
    """Whether the database refused evidence (MAT-03, MAT-05), as opposed to failing in some other way."""
    message = getattr(getattr(error.orig, "diag", None), "message_primary", None) or ""
    return message.startswith(REFUSALS)


def _insert_scored(
    conn: Connection, run_id: UUID, user_id: UUID, day: date, d: _Decision, position: int | None
) -> bool:
    """A scored decision and its evidence, in a savepoint. False when the database refuses it (a cited fact was
    superseded since scoring, or the evidence is incomplete); nothing of it is then left in the transaction. For a shown
    decision the completeness trigger is deferred to commit, so it is run here, at once, and put back."""
    try:
        with conn.begin_nested():
            decision_id = _insert_decision(conn, run_id, user_id, day, d, position)
            _write_evidence(conn, decision_id, user_id, d.rows)
            if d.outcome == "shown":
                conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
                conn.execute(text("SET CONSTRAINTS match_decisions_complete DEFERRED"))
    except DBAPIError as error:
        if _refused(error):
            return False
        raise
    return True


def _record(conn: Connection, run_id: UUID, user_id: UUID, day: date, plan: _Day) -> tuple[int, int]:
    """Write the decisions; returns (shown, withheld). A scored decision the database refuses is written again as
    withheld citation_unresolved, with no evidence, and a refused shown job does not use up its rank position."""
    free, shown, withheld = list(plan.free_positions), 0, 0
    for d in plan.decisions:
        if d.rows:
            position = free[0] if d.outcome == "shown" else None
            if _insert_scored(conn, run_id, user_id, day, d, position):
                if d.outcome == "shown":
                    free.pop(0)
                    shown += 1
                    if "material_change" in d.reasons:
                        log.event("match.reshown", job_id=d.job.job_id, content_version=d.job.content_version)
                else:
                    withheld += 1
                continue
            d = _Decision(d.job, "withheld", ("citation_unresolved",))
        _insert_decision(conn, run_id, user_id, day, d, None)
        if d.outcome == "withheld":
            withheld += 1
    return shown, withheld


def _write(user_id: UUID, day: date, prepared: _Prepared, scores: _Scores, now: datetime) -> RunResult:
    with db.transaction(user_id=user_id, role="match") as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"match.run:{user_id}"})
        status = conn.execute(
            text("SELECT status::text FROM match_runs WHERE id = :r FOR UPDATE"), {"r": prepared.run_id}
        ).scalar_one()
        if status == "completed":  # another attempt of this same run finished while this one scored
            return _result(conn, prepared.run_id)
        plan = _decide(conn, user_id, day, prepared, scores, now)
        shown, _ = _record(conn, prepared.run_id, user_id, day, plan)
        conn.execute(
            text(
                """UPDATE match_runs SET status = 'completed', finished_at = now(), retrieved_count = :retrieved,
                          filtered_count = :filtered, shown_count = :shown, evidence_prompt_version = :prompt,
                          error_code = :error
                    WHERE id = :r"""
            ),
            {
                "r": prepared.run_id,
                "retrieved": len(prepared.retrieved),
                "filtered": prepared.filtered_total + plan.filed,
                "shown": shown,
                "prompt": evidence.PROMPT_VERSION if scores.answered else None,
                "error": CHECKER_DISAGREEMENT if plan.disagreed else None,
            },
        )
        return _result(conn, prepared.run_id)


def _fail(user_id: UUID, run_id: UUID, code: str) -> None:
    """Mark the run failed in a transaction of its own, so none is left running."""
    with db.transaction(user_id=user_id, role="match") as conn:
        conn.execute(
            text(
                """UPDATE match_runs SET status = 'failed', finished_at = now(), error_code = :c
                    WHERE id = :r AND status = 'running'"""
            ),
            {"c": code, "r": run_id},
        )


def run(user_id: UUID, *, trigger: str, local_date: date | None = None, score_limit: int = SCORE_LIMIT) -> RunResult:
    """One matching run for a person (the match.api.run of the Phase 4 contract). `local_date` defaults to today in
    their time zone. Raises NotRunnable for a person with no active account, ValueError for a trigger that is not a
    candidate's, ProviderUnavailable when the model provider is down (the run is marked failed with that code, and the
    queue tries again), and any other error after marking the run failed as internal."""
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}")
    started, now = time.monotonic(), datetime.now(timezone.utc)
    zone, display_name, agreed = _account(user_id)
    day = local_date or now.astimezone(ZoneInfo(zone)).date()
    prepared = _prepare(user_id, trigger, day, score_limit, now)
    if isinstance(prepared, RunResult):
        return prepared  # a nightly run already done for this date
    try:
        names = (display_name,) if display_name else ()
        scores = _score(user_id, prepared.to_score, prepared.facts, names, agreed)
        result = _write(user_id, day, prepared, scores, now)
    except ProviderUnavailable:
        _fail(user_id, prepared.run_id, "provider_unavailable")
        raise
    except Exception:
        _fail(user_id, prepared.run_id, "internal")
        raise
    log.event(
        "match.run",
        run_id=result.run_id,
        kind=trigger,
        outcome=result.checker,
        retrieved=len(prepared.retrieved),
        scored=len(scores.accepted) + len(scores.rejected),
        shown=result.shown,
        withheld=result.withheld,
        filtered=result.filtered,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result
