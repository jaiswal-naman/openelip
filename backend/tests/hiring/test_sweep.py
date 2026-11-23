"""HIR-04 and HIR-06 (plan 06-05, task 2): the 15-minute hiring.sweep expires silence, schedules role runs and resumes
asking after an overdue answer is given. Everything is committed and invented (tests/hiring/factories.py)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.hiring import ask_jobs
from app.match.api import run_role
from app.platform import queue, scheduler
from tests.hiring import asks_world
from tests.hiring import factories as hiring
from tests.hiring import world as hiring_world
from tests.hiring.asks_world import introductions, make_candidate, make_role, role_runs, worker
from tests.match.fixtures.run import world


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    yield from asks_world.committed(engine, monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    return asks_world.provider(monkeypatch)


def sweep():
    worker(ask_jobs.sweep, {})


def ask_jobs_of(engine, role):
    return world.fetch(
        engine,
        "SELECT kind, queue::text AS queue, unique_key, status::text AS status FROM queue_jobs WHERE kind = :k "
        "AND payload->>'role_id' = :r ORDER BY id",
        k=ask_jobs.ASK_KIND,
        r=str(role.id),
    )


def finished_run(engine, role, *, hours_ago=0.0, status="completed"):
    with engine.begin() as c:
        return c.execute(
            text(
                """INSERT INTO match_runs (trigger, role_id, local_date, status, ranker_version, started_at,
                                          finished_at)
                   VALUES ('role', :r, current_date, CAST(:s AS match_run_status), 'test',
                           now() - make_interval(hours => :h), CASE WHEN CAST(:s AS text) = 'running' THEN NULL
                                                                    ELSE now() - make_interval(hours => :h) END)
                   RETURNING id"""
            ),
            {"r": role.id, "s": status, "h": hours_ago},
        ).scalar_one()


def overdue_org(engine):
    """An organization whose accepted candidate is owed an answer past due, with one more open role to ask for."""
    with engine.begin() as c:
        org = hiring.make_org(c)
    first, second = make_role(engine, org=org), make_role(engine, org=org)
    with engine.begin() as c:
        accepted = hiring.make_accepted(c, first, hiring.make_candidate(c))
        hiring_world.overdue(c, accepted.id)
    return org, second, accepted


def answer(engine, org, accepted):
    with engine.begin() as c:
        c.execute(
            text(
                """UPDATE introductions SET employer_answer = 'no', employer_answer_reason = 'Not now.',
                          employer_answered_by = :u, employer_answered_at = now() WHERE id = :i"""
            ),
            {"u": org.owner, "i": accepted.id},
        )


# --- expiry ---


def test_an_ask_older_than_seven_days_expires_and_a_younger_one_stays_asked(engine, provider):
    provider()
    role = make_role(engine)
    old, young = make_candidate(engine), make_candidate(engine)
    with engine.begin() as c:
        now = datetime.now(timezone.utc)
        hiring.make_ask(c, role, old, asked_at=now - timedelta(days=8))
        hiring.make_ask(c, role, young, asked_at=now - timedelta(days=6))

    sweep()

    by_user = {i.user_id: i.status for i in introductions(engine, role)}
    assert by_user == {old.id: "expired", young.id: "asked"}


def test_an_expired_ask_can_no_longer_be_accepted(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        ask = hiring.make_ask(c, role, ada, asked_at=datetime.now(timezone.utc) - timedelta(days=8))
    sweep()

    with pytest.raises(Exception) as refused, engine.begin() as c:
        c.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": ask.id})

    assert "cannot move from expired" in refused.value.orig.diag.message_primary


# --- scheduling ---


def test_an_open_role_with_no_run_today_gets_one_ask_job_and_a_second_sweep_adds_none(engine, provider):
    provider()
    role = make_role(engine)

    sweep()
    sweep()

    (job,) = ask_jobs_of(engine, role)
    slot = scheduler.slot_of(datetime.now(timezone.utc), ask_jobs.ROLE_RUN_INTERVAL_SECONDS)
    assert (job.queue, job.unique_key, job.status) == ("llm", f"hiring.ask:{role.id}:{slot}", "queued")


def test_no_ask_job_while_a_run_started_in_the_last_day_is_running_or_an_ask_job_is_unfinished(engine, provider):
    provider()
    recent, running, queued = make_role(engine), make_role(engine), make_role(engine)
    finished_run(engine, recent, hours_ago=3)
    finished_run(engine, running, hours_ago=30, status="running")
    with engine.begin() as c:
        queue.enqueue(
            c, ask_jobs.ASK_KIND, {"role_id": str(queued.id)}, unique_key=ask_jobs.ask_key(queued.id, "published")
        )

    sweep()

    assert ask_jobs_of(engine, recent) == [] and ask_jobs_of(engine, running) == []
    assert len(ask_jobs_of(engine, queued)) == 1  # the published one only


def test_a_run_older_than_a_day_is_due_again(engine, provider):
    provider()
    role = make_role(engine)
    finished_run(engine, role, hours_ago=25)

    sweep()

    assert len(ask_jobs_of(engine, role)) == 1


def test_a_closed_or_draft_role_is_not_scheduled_and_nothing_is_asked_for_it(engine, provider):
    provider()
    closed = make_role(engine)
    make_candidate(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET status = 'closed', closed_at = now() WHERE id = :r"), {"r": closed.id})
        org = hiring.make_org(c)
        draft = hiring.make_role(c, org)

    sweep()

    assert ask_jobs_of(engine, closed) == [] and ask_jobs_of(engine, draft) == []
    assert introductions(engine, closed) == [] and role_runs(engine, closed) == []


# --- overdue answers pause asking, and answering resumes it ---


def test_nobody_is_scheduled_or_asked_while_an_answer_is_overdue_and_asking_resumes_after_it_is_answered(
    engine, provider
):
    provider()
    org, role, accepted = overdue_org(engine)
    candidate = make_candidate(engine)
    run = run_role(role.id, role.job_id, [candidate.id])  # a run made before the pause is felt, with one selection
    assert len(run.selected) == 1

    sweep()

    assert ask_jobs_of(engine, role) == []  # not scheduled
    assert introductions(engine, role) == []  # and the held-back selection is not asked

    answer(engine, org, accepted)
    sweep()

    assert [i.user_id for i in introductions(engine, role)] == [candidate.id]  # within one sweep of the answer


def test_a_selection_is_asked_only_once_however_often_the_sweep_runs(engine, provider):
    provider()
    role = make_role(engine)
    candidate = make_candidate(engine)
    run_role(role.id, role.job_id, [candidate.id])

    sweep()
    sweep()

    assert [i.user_id for i in introductions(engine, role)] == [candidate.id]
