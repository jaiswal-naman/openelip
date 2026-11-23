"""The reads other modules need from the match ledger (re-exported by app.match.api; the Phase 4 index lists them):

  brief_matches(conn, user_id, local_date)   what a day's brief lists: the shown decisions, and, when no run that day
                                              could score, a few jobs marked "not yet assessed"
  activity_since(conn, user_id, since)        the completed runs after a time: what each one showed, withheld, filtered
  nightly_status(conn, user_id, local_date)   whether the day's nightly run is still to come, running, done or failed

Each takes an open connection and returns plain frozen values, so the caller owns the transaction. They run as the API
role (row-level security fences them to the person) and as the worker. The job's title and company are read in the
same SQL: the match module may read the job index in SQL, as DATA_MODEL 6.1 does.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Connection, text

NightlyStatus = Literal["none", "queued", "running", "completed", "failed"]

# How many jobs a brief lists as "not yet assessed" when no model could score them (INDEX deviation 8). An assumption:
# the brief is a short list, and five is the most a scored day shows.
NOT_ASSESSED_LIMIT = 5  # assumption

# A decision records what a run decided; whether its job may still be shown is asked again whenever the decision is read
# (invariant 1), of the same SQL functions every run filters with (DATA_MODEL 4.1), against the person's constraints as
# they are now. A constraint confirmed after the run, a changed location, a company the person has just excluded: each
# holds at once, not at the next run. The three pieces belong to a query over `match_decisions d JOIN jobs j`:
#   CURRENT_CONSTRAINTS   the CTE `c` (and its :u parameter), joined with CROSS JOIN c
#   STILL_ALLOWED         true for a job that breaks no hard constraint (retrieval.py's own predicate)
#   UNVERIFIED_NOW        the constrained fields the posting leaves unstated, as text[]
# The ledger (ledger.py) reads its matches the same way, so the list, the page and the brief agree.
CURRENT_CONSTRAINTS = "WITH c AS (SELECT compile_constraints(:u) AS cc)"
STILL_ALLOWED = "constraint_violations(j, c.cc) = '{}'"
UNVERIFIED_NOW = "CAST(unverified_fields(j, c.cc) AS text[])"


@dataclass(frozen=True)
class BriefMatch:
    """One line of a brief. A job nobody assessed has no summary and no rank, and says so with is_assessed False."""

    decision_id: UUID
    job_id: int
    title: str
    company_name: str
    summary: str | None
    rank_position: int | None
    change_note: str | None
    is_assessed: bool
    unverified: tuple[str, ...]  # constrained fields the posting does not state, shown beside the match


@dataclass(frozen=True)
class RunActivity:
    """What one finished run did. filtered counts every job the constraints removed, stored or not."""

    run_id: UUID
    trigger: str
    finished_at: datetime
    shown: int
    withheld: int
    filtered: int


def nightly_key(user_id: UUID, local_date: date) -> str:
    """The queue's unique key of a candidate's nightly run on a local date (ARCHITECTURE 5.3): the producer and the
    status read below name the same job by this one function."""
    return f"match.nightly:{user_id}:{local_date.isoformat()}"


def brief_matches(conn: Connection, user_id: UUID, local_date: date) -> list[BriefMatch]:
    """The shown decisions of that local date by rank, then, only if no run of that date scored (so no job could be
    shown), up to NOT_ASSESSED_LIMIT jobs the runs retrieved but could not assess, best retrieval rank first.

    A job two unscored runs both retrieved is listed once. The unassessed ones carry no summary: nothing was said
    about them, only that the rules ranked them high (AI_SYSTEM 2.10).

    Both lists are read as of now, not as of the run: a job that has closed since, or that the person's hard constraints
    now exclude, is left out, and the "unverified" fields are those of the constraints the person has now. The email is
    written from this when it is sent, so it cannot carry what the person has excluded since the brief was composed.
    """
    shown = conn.execute(
        text(
            f"""{CURRENT_CONSTRAINTS}
               SELECT d.id, d.job_id, j.title, j.company_name, d.summary, d.rank_position, d.change_note,
                      {UNVERIFIED_NOW} AS unverified
               FROM match_decisions d JOIN jobs j ON j.id = d.job_id CROSS JOIN c
               WHERE d.user_id = :u AND d.local_date = :d AND d.outcome = 'shown'
                 AND j.status = 'open' AND {STILL_ALLOWED}
               ORDER BY d.rank_position"""
        ),
        {"u": user_id, "d": local_date},
    ).all()
    not_assessed = conn.execute(
        text(
            f"""{CURRENT_CONSTRAINTS}
               SELECT t.id, t.job_id, t.title, t.company_name, t.unverified FROM (
                   SELECT DISTINCT ON (d.job_id) d.id, d.job_id, j.title, j.company_name, d.retrieval_rank,
                          {UNVERIFIED_NOW} AS unverified
                   FROM match_decisions d JOIN jobs j ON j.id = d.job_id CROSS JOIN c
                   WHERE d.user_id = :u AND d.local_date = :d AND d.outcome = 'withheld'
                     AND 'not_scored' = ANY (d.reason_codes)
                     AND NOT EXISTS (SELECT 1 FROM match_runs r WHERE r.user_id = :u AND r.local_date = :d
                                                                  AND r.evidence_prompt_version IS NOT NULL)
                     AND j.status = 'open' AND {STILL_ALLOWED}
                   ORDER BY d.job_id, d.retrieval_rank NULLS LAST, d.id
               ) t
               ORDER BY t.retrieval_rank NULLS LAST, t.id
               LIMIT :limit"""
        ),
        {"u": user_id, "d": local_date, "limit": NOT_ASSESSED_LIMIT},
    ).all()
    matches = [
        BriefMatch(
            r.id,
            r.job_id,
            r.title,
            r.company_name,
            r.summary,
            r.rank_position,
            r.change_note,
            True,
            tuple(r.unverified),
        )
        for r in shown
    ]
    matches += [
        BriefMatch(r.id, r.job_id, r.title, r.company_name, None, None, None, False, tuple(r.unverified))
        for r in not_assessed
    ]
    return matches


def activity_since(conn: Connection, user_id: UUID, since: datetime) -> list[RunActivity]:
    """The person's completed runs that finished after `since` (an aware datetime), oldest first, for "done since the
    last brief" (BRF-02). A run that is still running or failed did nothing the person can be told about."""
    rows = conn.execute(
        text(
            """SELECT r.id, r.trigger, r.finished_at, r.shown_count AS shown, r.filtered_count AS filtered,
                      (SELECT count(*) FROM match_decisions d
                       WHERE d.match_run_id = r.id AND d.outcome = 'withheld') AS withheld
               FROM match_runs r
               WHERE r.user_id = :u AND r.status = 'completed' AND r.finished_at > :since
               ORDER BY r.finished_at, r.id"""
        ),
        {"u": user_id, "since": since},
    ).all()
    return [RunActivity(r.id, r.trigger, r.finished_at, r.shown, r.withheld, r.filtered) for r in rows]


def nightly_status(conn: Connection, user_id: UUID, local_date: date) -> NightlyStatus:
    """Where the day's nightly run is: what its run row says if it has one; "queued" while its queue job is waiting or
    running and has made no run row yet; "none" if nothing was ever asked for. The brief waits for "queued" and
    "running" (up to its deadline) and composes with what exists otherwise."""
    status = conn.execute(
        text("SELECT status FROM match_runs WHERE user_id = :u AND local_date = :d AND trigger = 'nightly'"),
        {"u": user_id, "d": local_date},
    ).scalar()
    if status is not None:
        return cast(NightlyStatus, status)
    waiting = conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM queue_jobs WHERE unique_key = :key AND status IN ('queued', 'running'))"),
        {"key": nightly_key(user_id, local_date)},
    ).scalar_one()
    return "queued" if waiting else "none"
