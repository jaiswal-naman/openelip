"""Invariant 6 for asks and brief items (plan 06-05, task 3): nothing is ever produced twice, however often a job runs.

Each guard has its own test, and the whole flow runs at the end: the ask job, the sweep and the compose, each twice.
Everything is committed and invented (tests/hiring/factories.py).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.brief.api import compose
from app.hiring import ask_jobs, asks
from app.match.api import run_role
from app.platform import db, queue
from app.trust import api as trust
from tests.hiring import asks_world
from tests.hiring.asks_world import introductions, make_candidate, make_role, role_runs, worker
from tests.match.factories import LOCAL_DATE
from tests.match.fixtures.run import world

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    yield from asks_world.committed(engine, monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    return asks_world.provider(monkeypatch)


def run_job(role):
    worker(ask_jobs.ask_candidates, {"role_id": str(role.id)})


def test_running_the_ask_job_twice_asks_each_candidate_once(engine, provider):
    provider()
    role = make_role(engine)
    people = [make_candidate(engine) for _ in range(2)]

    run_job(role)
    run_job(role)

    assert sorted(i.user_id for i in introductions(engine, role)) == sorted(p.id for p in people)
    assert len(role_runs(engine, role)) == 1  # the second job had nobody left to ask, so it made no empty run


def test_asking_from_the_same_run_twice_asks_each_candidate_once(engine, provider):
    provider()
    role = make_role(engine)
    people = [make_candidate(engine) for _ in range(2)]
    result = run_role(role.id, role.job_id, [p.id for p in people])
    open_role = worker(asks.open_role, role.id)

    first = worker(asks.ask_from_run, open_role, result.run_id)
    second = worker(asks.ask_from_run, open_role, result.run_id)

    assert (first.asked, second.asked) == (2, 0)
    assert len(introductions(engine, role)) == 2


def test_two_jobs_that_race_still_make_one_ask_because_of_on_conflict_do_nothing(engine, provider, monkeypatch):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    result = run_role(role.id, role.job_id, [ada.id])
    open_role = worker(asks.open_role, role.id)
    monkeypatch.setattr(asks, "_already_asked", lambda *a: False)  # both jobs looked before either wrote

    first = worker(asks.ask_from_run, open_role, result.run_id)
    second = worker(asks.ask_from_run, open_role, result.run_id)

    assert (first.asked, second.asked) == (1, 0)
    assert len(introductions(engine, role)) == 1


def test_the_database_refuses_a_second_introduction_for_the_same_role_and_candidate(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    run_job(role)
    (first,) = introductions(engine, role)

    with pytest.raises(IntegrityError) as refused:
        with engine.begin() as c:
            c.execute(
                text(
                    """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview)
                       SELECT organization_id, role_id, user_id, match_decision_id, share_preview
                         FROM introductions WHERE id = :i"""
                ),
                {"i": first.id},
            )

    assert refused.value.orig.diag.constraint_name == "introductions_once" and first.user_id == ada.id


def test_running_the_sweep_twice_asks_each_candidate_once(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    run_role(role.id, role.job_id, [ada.id])

    worker(ask_jobs.sweep, {})
    worker(ask_jobs.sweep, {})

    assert [i.user_id for i in introductions(engine, role)] == [ada.id]


def test_enqueueing_the_publish_key_twice_makes_one_queue_row(engine, provider):
    provider()
    role = make_role(engine)
    key = ask_jobs.ask_key(role.id, "published")

    with engine.begin() as c:
        first = queue.enqueue(c, ask_jobs.ASK_KIND, {"role_id": str(role.id)}, unique_key=key, queue=ask_jobs.ASK_QUEUE)
        second = queue.enqueue(
            c, ask_jobs.ASK_KIND, {"role_id": str(role.id)}, unique_key=key, queue=ask_jobs.ASK_QUEUE
        )

    assert first is not None and second is None
    assert len(world.fetch(engine, "SELECT 1 FROM queue_jobs WHERE unique_key = :k", k=key)) == 1


def test_the_whole_flow_twice_gives_one_ask_one_brief_item_and_one_brief_email_per_brief(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine, email="reader@mail.test", is_synthetic=False)
    with engine.begin() as c:
        trust.grant_consent(c, ada.id, "brief_email", "test-wording")

    for _ in range(2):
        run_job(role)
        worker(ask_jobs.sweep, {})
    (ask,) = introductions(engine, role)
    morning = lambda day: compose_once(engine, ada.id, day)  # noqa: E731

    first = [morning(LOCAL_DATE), morning(LOCAL_DATE)]  # the same date and channel twice
    later = morning(LOCAL_DATE + timedelta(days=1))  # and the next day, with the ask still open

    assert first[0] == first[1]
    items = world.fetch(
        engine,
        """SELECT b.local_date, b.channel::text AS channel, i.introduction_id FROM brief_items i
             JOIN briefs b ON b.id = i.brief_id WHERE i.kind = 'introduction'""",
    )
    assert [(i.local_date, i.channel, i.introduction_id) for i in items] == [(LOCAL_DATE, "in_app", ask.id)]
    assert later.status == "suppressed" and later.outbox_id is None  # the ask is not repeated, so nothing is to say
    outbox = world.fetch(engine, "SELECT dedupe_key FROM outbox WHERE dedupe_key LIKE 'brief:%' ORDER BY dedupe_key")
    assert [o.dedupe_key for o in outbox] == [f"brief:{first[0].email_brief_id}"]  # one email for one brief


def compose_once(engine, user_id, day):
    with db.transaction(role="worker") as conn:
        return compose(conn, user_id, day, now=MORNING + timedelta(days=(day - LOCAL_DATE).days))
