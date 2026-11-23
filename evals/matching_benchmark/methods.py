"""The six methods over one profile (README "The six methods").

M1 to M4 call no model: they return the jobs of the person's own record in rank order, after the constraint filter that
sits inside match.api.retrieve. M5 and M6 score jobs with the evidence task through the gateway; with no usable model
they raise ModelNotAvailable and the method is reported as not run. Neither writes a decision or evidence row (only the
gateway's own ledger of calls, which is where the cost comes from).

The benchmark measures what the nightly run executes, so it uses the run's own helpers. They are private to
app.match.service on purpose: a rename there stops this module at import, and nothing is ever measured against a copy.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Connection

from app.match import api as match
from app.match import evidence, ranking, retrieval, validator
from app.match.retrieval import Candidate
from app.match.service import _checker_constraints, _final_score, _prerank, _to_score
from app.platform import config
from app.platform.llm import AIDisabled, SchemaInvalid, registry
from app.profile import api as profile

LIST_LIMIT = 300  # jobs M1 to M3 return (AI_SYSTEM 3.3)
PRERANK_FROM = 50  # jobs M4 re-orders: what a run takes from the fused list (DATA_MODEL 6.1)
SCORE_LIMIT = 40  # jobs M5 scores per profile (ARCHITECTURE 5.8)
SHOWN = 5  # jobs M6 scores: the day's cap
DAILY_CAP = 5


class ModelNotAvailable(Exception):
    """No model can be called (no key, no route, or the fake provider has no recording). str() is the gateway's code."""


class RequirementsMissing(Exception):
    """None of the jobs to score has extracted requirements, so there is nothing to ask a model about. Extraction is a
    model-backed step of its own (ingest.extract), which a benchmark database has not had."""


@dataclass(frozen=True)
class Scoring:
    """What scoring a list of jobs gave: the verdict sets that passed the validator by job id, and the counts of the
    rest (a set refused twice, a job whose answer was not valid twice)."""

    accepted: dict[int, validator.Accepted]
    scored: list[Candidate]  # the jobs asked about, in the order they were asked
    rejected: int
    schema_invalid: int


def keyword(conn: Connection, user_id: UUID) -> list[Candidate]:
    """M1."""
    return match.retrieve(conn, user_id, mode="keyword", limit=LIST_LIMIT, depth=LIST_LIMIT)


def vector(conn: Connection, user_id: UUID) -> list[Candidate]:
    """M2."""
    return match.retrieve(conn, user_id, mode="vector", limit=LIST_LIMIT, depth=LIST_LIMIT)


def hybrid(conn: Connection, user_id: UUID) -> list[Candidate]:
    """M3."""
    return match.retrieve(conn, user_id, mode="hybrid", limit=LIST_LIMIT, depth=LIST_LIMIT)


def hybrid_rules(conn: Connection, user_id: UUID, now: datetime) -> list[Candidate]:
    """M4: M3's first 50 in the order of the run's pre-rank (ties to the lower job id)."""
    return _prerank(hybrid(conn, user_id)[:PRERANK_FROM], retrieval.active_preferences(conn, user_id), now)


def violation_count(conn: Connection, user_id: UUID, jobs: Sequence[Candidate]) -> int:
    """Jobs in the list for which the independent checker finds a hard constraint broken (the bar is 0)."""
    constraints = _checker_constraints(profile.compiled_constraints(conn, user_id))
    return sum(bool(match.violations(job.facts, constraints)) for job in jobs)


def _verdicts(request: evidence.EvidenceRequest, user_id: UUID) -> validator.Accepted | validator.Rejected:
    """A verdict set that passed the validator, or the second rejection: a rejected set gets one fresh call (AI_SYSTEM
    4.4), as the run does."""
    for _ in range(2):
        result = validator.validate(
            evidence.score(request, principal=user_id), request, career_months=request.career_months
        )
        if isinstance(result, validator.Accepted):
            return result
    return result


def score_jobs(conn: Connection, user_id: UUID, ranked: Sequence[Candidate], *, limit: int, today: date) -> Scoring:
    """Score the best `limit` of `ranked` that have extracted requirements, one request per job, as the run does.
    Raises ModelNotAvailable when the gateway has no usable route for the task (checked before anything is asked) or
    answers AIDisabled, and RequirementsMissing when no job has requirements; a ProviderUnavailable is not caught, so
    the caller stops."""
    try:
        registry.route(evidence.TASK, config.get())
    except AIDisabled as disabled:
        raise ModelNotAvailable(disabled.reason) from disabled
    facts = profile.usable_facts(conn, user_id)
    items = _to_score(conn, list(ranked), limit)
    if ranked and not items:
        raise RequirementsMissing("no job in the list has extracted requirements")
    accepted: dict[int, validator.Accepted] = {}
    rejected = schema_invalid = 0
    for item in items:
        # names=(): the profiles are invented, and "Demo" is a word in their invented employers' names
        request = evidence.build_request(facts, item.requirements, description=item.description, today=today)
        try:
            outcome = _verdicts(request, user_id)
        except AIDisabled as disabled:
            raise ModelNotAvailable(disabled.reason) from disabled
        except SchemaInvalid:
            schema_invalid += 1
            continue
        if isinstance(outcome, validator.Accepted):
            accepted[item.job.job_id] = outcome
        else:
            rejected += 1
    return Scoring(accepted, [item.job for item in items], rejected, schema_invalid)


def full_cascade(
    conn: Connection, user_id: UUID, m4: Sequence[Candidate], *, now: datetime
) -> tuple[list[Candidate], Scoring, int]:
    """M5: M4's first 40 that have requirements, scored, then ordered by the rules' final score (ties to the lower job
    id). Returns the ordered jobs that have a validated verdict set, the scoring, and how many of them the rules would
    show today (ranking.pick, at most 5)."""
    scoring = score_jobs(conn, user_id, m4, limit=SCORE_LIMIT, today=now.date())
    preferences = retrieval.active_preferences(conn, user_id)
    scored = [
        ranking.Scored(
            job.job_id,
            job.facts.company_name,
            job.title,
            _final_score(job, scoring.accepted[job.job_id].rows, preferences, now),
            scoring.accepted[job.job_id].rows,
            scoring.accepted[job.job_id].weak_fit,
        )
        for job in scoring.scored
        if job.job_id in scoring.accepted
    ]
    by_id = {job.job_id: job for job in scoring.scored}
    order = [by_id[s.job_id] for s in sorted(scored, key=lambda s: (-s.final, s.job_id))]
    return order, scoring, len(ranking.pick(scored, DAILY_CAP).shown)


def score_shown(
    conn: Connection, user_id: UUID, m4: Sequence[Candidate], *, now: datetime
) -> tuple[list[Candidate], Scoring]:
    """M6: M4's first 5 are scored for their requirement tables only, so the list is M4's own, in M4's order."""
    shown = list(m4[:SHOWN])
    return shown, score_jobs(conn, user_id, shown, limit=SHOWN, today=now.date())
