"""The match module's reads for the brief (plan 04-01): brief_matches, activity_since and nightly_status.

Rows are invented (factories.py) and written on the `conn` fixture's connection, which is rolled back. The last test
runs the same reads as the two roles that call them, the API (fenced to one person) and the worker.
"""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, text

from app.match import api
from app.match.queries import NOT_ASSESSED_LIMIT, nightly_key
from app.platform import queue
from tests.match.factories import (
    LOCAL_DATE,
    Job,
    Verdict,
    make_decision,
    make_job,
    make_run,
    make_shown_decision,
    make_user,
)
from tests.match.fixtures.run.world import add_constraint

TOMORROW = LOCAL_DATE + timedelta(days=1)


def make_titled_job(conn: Connection, title: str, **fields) -> Job:
    return make_job(conn, title=title, description="Python.", requirements=[("Python.", "required")], **fields)


def show(conn: Connection, run: UUID, user: UUID, job: Job, rank: int, **fields) -> UUID:
    return make_shown_decision(conn, run, user, job, [Verdict("no_evidence")], rank_position=rank, **fields)


def not_scored(conn: Connection, run: UUID, user: UUID, job: Job, rank: int | None, **fields) -> UUID:
    return make_decision(conn, run, user, job, retrieval_rank=rank, **fields)


# ---- brief_matches ---------------------------------------------------------------------------------------------


def test_the_shown_decisions_of_the_date_are_listed_by_rank_with_the_job_and_the_note(conn):
    ada, bob = make_user(conn), make_user(conn)
    add_constraint(conn, ada, "salary_floor", salary_floor=3_000_000, salary_currency="INR")  # what the flags are about
    first = make_titled_job(conn, "Data engineer")
    second = make_titled_job(
        conn,
        "Platform engineer",
        company_name="Hooli Demo",
        salary_min=3_000_000,
        salary_max=4_000_000,
        salary_currency="INR",
    )
    third = make_titled_job(conn, "Analyst")
    run = make_run(conn, ada)
    show(conn, run, ada, second, 2, summary="Runs the platform.")
    top = show(conn, run, ada, first, 1, summary="Builds pipelines.", change_note="Pay is now stated.")
    not_scored(conn, run, ada, third, 3, reasons=("weak_fit",))  # held back by a scored run: not on the brief
    show(conn, make_run(conn, ada, local_date=TOMORROW), ada, third, 1)  # another date
    show(conn, make_run(conn, bob), bob, first, 1)  # another person

    matches = api.brief_matches(conn, ada, LOCAL_DATE)

    assert [m.title for m in matches] == ["Data engineer", "Platform engineer"]
    assert [m.rank_position for m in matches] == [1, 2]
    assert [m.company_name for m in matches] == ["Initech Demo", "Hooli Demo"]
    assert [m.job_id for m in matches] == [first.id, second.id]
    assert all(m.is_assessed for m in matches)
    top_match = matches[0]
    assert (top_match.decision_id, top_match.summary) == (top, "Builds pipelines.")
    assert (top_match.change_note, top_match.unverified) == ("Pay is now stated.", ("salary_not_stated",))
    assert (matches[1].change_note, matches[1].unverified) == (None, ())


def test_a_day_with_no_shown_match_and_a_scored_run_lists_nothing(conn):
    """Zero matches is an answer: a run that could score and found nothing worth showing is not "not assessed"."""
    ada = make_user(conn)
    run = make_run(conn, ada)
    not_scored(conn, run, ada, make_titled_job(conn, "Analyst"), 1, reasons=("weak_fit",))
    assert api.brief_matches(conn, ada, LOCAL_DATE) == []


def test_when_no_run_of_the_day_could_score_the_best_ranked_unassessed_jobs_are_listed_marked_so(conn):
    ada, bob = make_user(conn), make_user(conn)
    add_constraint(conn, ada, "location", country_codes=["IN"])  # the postings state no country: the flag below
    run = make_run(conn, ada, evidence_prompt_version=None)  # no model: nothing was scored, so nothing was shown
    jobs = [make_titled_job(conn, f"Job {n}") for n in range(NOT_ASSESSED_LIMIT + 2)]
    for rank, job in zip((6, 2, 7, 1, 5, 3, 4), jobs, strict=True):  # shuffled, so the order below is the rank's
        not_scored(conn, run, ada, job, rank)
    not_scored(conn, run, ada, make_titled_job(conn, "Weak"), 1, reasons=("weak_fit",))  # a different reason
    not_scored(conn, make_run(conn, ada, local_date=TOMORROW, evidence_prompt_version=None), ada, jobs[0], 1)
    not_scored(conn, make_run(conn, bob, evidence_prompt_version=None), bob, jobs[0], 1)

    matches = api.brief_matches(conn, ada, LOCAL_DATE)

    assert [m.title for m in matches] == ["Job 3", "Job 1", "Job 5", "Job 6", "Job 4"]  # ranks 1, 2, 3, 4, 5
    assert not any(m.is_assessed for m in matches)
    assert {(m.summary, m.rank_position, m.change_note) for m in matches} == {(None, None, None)}
    assert matches[0].unverified == ("location_not_stated",)


def test_a_job_two_unscored_runs_both_retrieved_is_listed_once_at_its_best_rank(conn):
    ada = make_user(conn)
    twice, once = make_titled_job(conn, "Twice"), make_titled_job(conn, "Once")
    onboarding = make_run(conn, ada, trigger="onboarding", evidence_prompt_version=None)
    nightly = make_run(conn, ada, evidence_prompt_version=None)
    not_scored(conn, onboarding, ada, twice, 5)
    not_scored(conn, onboarding, ada, once, 2)
    not_scored(conn, nightly, ada, twice, 1)
    assert [m.title for m in api.brief_matches(conn, ada, LOCAL_DATE)] == ["Twice", "Once"]


def test_one_scored_run_in_the_day_means_no_unassessed_jobs_at_all(conn):
    ada = make_user(conn)
    earlier = make_run(conn, ada, trigger="onboarding", evidence_prompt_version=None)  # before a model could be used
    not_scored(conn, earlier, ada, make_titled_job(conn, "Old news"), 1)
    nightly = make_run(conn, ada)  # the nightly run could score
    top = show(conn, nightly, ada, make_titled_job(conn, "Scored"), 1)
    assert [(m.decision_id, m.is_assessed) for m in api.brief_matches(conn, ada, LOCAL_DATE)] == [(top, True)]


# ---- what may be listed now: a decision is read against today's constraints and today's index ------------------


def test_a_shown_job_the_constraints_now_exclude_is_left_off_the_brief(conn):
    ada = make_user(conn)
    run = make_run(conn, ada)
    stays = show(conn, run, ada, make_titled_job(conn, "Data engineer", country="IN"), 1)
    show(conn, run, ada, make_titled_job(conn, "Platform engineer", company_name="Hooli Demo", country="IN"), 2)
    assert len(api.brief_matches(conn, ada, LOCAL_DATE)) == 2  # no constraint yet

    add_constraint(conn, ada, "excluded_company", company_names=["Hooli Demo"])  # confirmed after the run

    assert [(m.decision_id, m.title) for m in api.brief_matches(conn, ada, LOCAL_DATE)] == [(stays, "Data engineer")]


def test_a_location_changed_after_the_run_leaves_off_the_jobs_it_now_excludes_and_flags_the_ones_that_do_not_say(conn):
    ada = make_user(conn)
    run = make_run(conn, ada)
    show(conn, run, ada, make_titled_job(conn, "Data engineer", country="IN"), 1)
    stays = show(conn, run, ada, make_titled_job(conn, "Remote analyst"), 2)  # the posting names no country

    add_constraint(conn, ada, "location", country_codes=["DE"])  # she is moving to Germany

    (left,) = api.brief_matches(conn, ada, LOCAL_DATE)
    assert (left.decision_id, left.title) == (stays, "Remote analyst")
    assert left.unverified == ("location_not_stated",)  # it passes because it is silent, and the brief says so


def test_a_job_nobody_assessed_that_the_constraints_now_exclude_is_not_listed_either(conn):
    ada = make_user(conn)
    run = make_run(conn, ada, evidence_prompt_version=None)
    not_scored(conn, run, ada, make_titled_job(conn, "Fine", country="IN"), 1)
    not_scored(conn, run, ada, make_titled_job(conn, "Excluded", company_name="Hooli Demo", country="IN"), 2)

    add_constraint(conn, ada, "excluded_company", company_names=["Hooli Demo"])

    assert [(m.title, m.is_assessed) for m in api.brief_matches(conn, ada, LOCAL_DATE)] == [("Fine", False)]


def close(conn: Connection, job: Job) -> None:
    """The posting is taken down (the six-hourly refresh does this between a run and the morning brief)."""
    conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": job.id})


def test_a_job_that_has_closed_since_the_run_is_left_off_the_brief(conn):
    ada = make_user(conn)
    run = make_run(conn, ada)
    stays = show(conn, run, ada, make_titled_job(conn, "Still open"), 1)
    gone = make_titled_job(conn, "Closed since")
    show(conn, run, ada, gone, 2)
    close(conn, gone)

    assert [m.decision_id for m in api.brief_matches(conn, ada, LOCAL_DATE)] == [stays]


def test_a_closed_job_nobody_assessed_is_not_listed_as_not_yet_assessed(conn):
    ada = make_user(conn)
    run = make_run(conn, ada, evidence_prompt_version=None)
    not_scored(conn, run, ada, make_titled_job(conn, "Still open"), 1)
    gone = make_titled_job(conn, "Closed since")
    not_scored(conn, run, ada, gone, 2)
    close(conn, gone)

    assert [m.title for m in api.brief_matches(conn, ada, LOCAL_DATE)] == ["Still open"]


# ---- activity_since --------------------------------------------------------------------------------------------


def test_activity_lists_the_completed_runs_after_the_time_with_what_each_one_did(conn):
    ada, bob = make_user(conn), make_user(conn)
    now = conn.execute(text("SELECT now()")).scalar_one()
    jobs = [make_titled_job(conn, f"Job {n}") for n in range(4)]

    def run(days_ago: float, **fields) -> UUID:
        return make_run(conn, ada, finished_at=now - timedelta(days=days_ago), **fields)

    run(3, local_date=LOCAL_DATE - timedelta(days=3))  # before the time asked for
    onboarding = run(0.5, trigger="onboarding", filtered_count=7)
    nightly = run(0.1, local_date=TOMORROW, filtered_count=40)
    run(0.05, local_date=TOMORROW + timedelta(days=1), status="failed")  # failed: nothing to report
    make_run(conn, ada, local_date=TOMORROW + timedelta(days=2), status="running")  # not finished
    make_run(conn, bob, finished_at=now - timedelta(hours=1))  # another person's
    show(conn, onboarding, ada, jobs[0], 1)
    not_scored(conn, onboarding, ada, jobs[1], 1)
    show(conn, nightly, ada, jobs[2], 2)
    not_scored(conn, nightly, ada, jobs[3], 2)
    not_scored(conn, nightly, ada, make_titled_job(conn, "Extra"), 3)
    make_decision(
        conn,
        nightly,
        ada,
        make_titled_job(conn, "Violator"),
        outcome="filtered",
        reasons=("hard_constraint",),
        violated_kinds=("location",),
    )

    activity = api.activity_since(conn, ada, now - timedelta(days=1))

    assert [(a.run_id, a.trigger) for a in activity] == [(onboarding, "onboarding"), (nightly, "nightly")]
    assert [(a.shown, a.withheld, a.filtered) for a in activity] == [(1, 1, 7), (1, 2, 40)]  # filtered: every violator
    assert activity[0].finished_at == now - timedelta(days=0.5)
    assert api.activity_since(conn, ada, now) == []


# ---- nightly_status --------------------------------------------------------------------------------------------


def test_the_queue_key_is_the_one_the_schedule_will_use():
    user = UUID("12345678-1234-5678-1234-567812345678")
    assert nightly_key(user, LOCAL_DATE) == "match.nightly:12345678-1234-5678-1234-567812345678:2026-11-23"


def test_nightly_status_follows_the_run_and_waits_in_the_queue_before_it_has_one(conn):
    ada, bob = make_user(conn), make_user(conn)

    def status(user=ada, day=LOCAL_DATE):
        return api.nightly_status(conn, user, day)

    assert status() == "none"

    queue.enqueue(conn, "match.nightly", {"user_id": str(ada)}, unique_key=nightly_key(ada, LOCAL_DATE), queue="llm")
    assert status() == "queued"  # asked for, nothing has started
    conn.execute(
        text("UPDATE queue_jobs SET status = 'running' WHERE unique_key = :k"), {"k": nightly_key(ada, LOCAL_DATE)}
    )
    assert status() == "queued"  # claimed, and no run row yet: still to come
    assert status(bob) == "none" and status(day=TOMORROW) == "none"  # nobody else's, and not another day's

    make_run(conn, ada, trigger="onboarding")  # an onboarding run is not the nightly one
    assert status() == "queued"
    run = make_run(conn, ada, status="running")
    assert status() == "running"
    conn.execute(text("UPDATE match_runs SET status = 'failed' WHERE id = :r"), {"r": run})
    assert status() == "failed"
    conn.execute(text("UPDATE match_runs SET status = 'completed' WHERE id = :r"), {"r": run})
    conn.execute(
        text("UPDATE queue_jobs SET status = 'succeeded', finished_at = now() WHERE unique_key = :k"),
        {"k": nightly_key(ada, LOCAL_DATE)},
    )
    assert status() == "completed"


def test_a_queue_job_that_finished_without_a_run_is_not_waiting(conn):
    ada = make_user(conn)
    queue.enqueue(conn, "match.nightly", {}, unique_key=nightly_key(ada, LOCAL_DATE), queue="llm")
    for finished in ("succeeded", "dead"):
        key = nightly_key(ada, LOCAL_DATE)
        conn.execute(
            text("UPDATE queue_jobs SET status = :s, finished_at = now() WHERE unique_key = :k"),
            {"s": finished, "k": key},
        )
        assert api.nightly_status(conn, ada, LOCAL_DATE) == "none"


# ---- the roles that call them ----------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["app_worker", "app_api"])
def test_the_reads_give_the_same_answers_to_the_worker_and_to_the_person_through_row_level_security(conn, role):
    ada, bob = make_user(conn), make_user(conn)
    now = conn.execute(text("SELECT now()")).scalar_one()
    run = make_run(conn, ada, filtered_count=3)
    show(conn, run, ada, make_titled_job(conn, "Mine"), 1, summary="Yours.")
    show(conn, make_run(conn, bob), bob, make_titled_job(conn, "Theirs"), 1)
    queue.enqueue(conn, "match.nightly", {}, unique_key=nightly_key(ada, TOMORROW), queue="llm")
    expected = (
        api.brief_matches(conn, ada, LOCAL_DATE),
        api.activity_since(conn, ada, now - timedelta(days=1)),
        api.nightly_status(conn, ada, LOCAL_DATE),
        api.nightly_status(conn, ada, TOMORROW),
    )
    assert [m.title for m in expected[0]] == ["Mine"] and expected[3] == "queued"

    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(ada) if role == "app_api" else ""})
    assert (
        api.brief_matches(conn, ada, LOCAL_DATE),
        api.activity_since(conn, ada, now - timedelta(days=1)),
        api.nightly_status(conn, ada, LOCAL_DATE),
        api.nightly_status(conn, ada, TOMORROW),
    ) == expected
