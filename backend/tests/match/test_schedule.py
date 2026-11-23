"""MAT-02 and BRF-03 for runs (plan 04-03, task 3): when matching happens. Time zones, the nightly plan, who is due, the
pacing rule, the first run after a record is confirmed, and how the handlers fail the queue.

Everything is invented. The handlers' work is in plain functions that take the time, so the tests set it. Every
test runs in the `conn` fixture's transaction, which is rolled back.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.match import jobs, service
from app.platform import queue, scheduler
from app.platform.llm import ProviderUnavailable
from app.platform.queue import NonRetryable, enqueue
from tests.match.factories import make_decision, make_job, make_record, make_run, make_user

DAY = date(2026, 11, 23)
PREVIOUS_DAY = date(2026, 11, 22)
KOLKATA = ZoneInfo("Asia/Kolkata")


def kolkata(hour, minute, day=DAY):
    """An aware time that is `hour:minute` on `day` in Asia/Kolkata (UTC+5:30), as the UTC the worker's clock gives."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KOLKATA).astimezone(timezone.utc)


def queued(conn, kind):
    return conn.execute(
        text(
            """SELECT unique_key, queue, payload, max_attempts, lease_seconds FROM queue_jobs
                WHERE kind = :k ORDER BY unique_key"""
        ),
        {"k": kind},
    ).all()


def person_of(row):
    """The person a nightly job is for, from its key (match.nightly:{user}:{date})."""
    return UUID(row.unique_key.split(":")[1])


def person(conn, **options):
    """An invented candidate with a confirmed record."""
    user = make_user(conn, **options)
    make_record(conn, user)
    return user


# --- match.zones -----------------------------------------------------------------------------------------------------


def test_a_zone_is_planned_once_local_time_passes_03_00(conn):
    person(conn)

    assert jobs.plan_zones(conn, kolkata(2, 59)) == 0 and queued(conn, "match.plan") == []
    assert jobs.plan_zones(conn, kolkata(3, 5)) == 1
    (plan,) = queued(conn, "match.plan")
    assert (plan.unique_key, plan.queue, plan.payload) == (
        "match.plan:Asia/Kolkata:2026-11-23",
        "default",
        {"zone": "Asia/Kolkata", "local_date": "2026-11-23"},
    )
    assert plan.max_attempts == jobs.PLAN_ATTEMPTS == 20  # so pacing can make it wait
    assert jobs.plan_zones(conn, kolkata(3, 6)) == 0  # a second tick, or another worker's, adds nothing
    assert jobs.plan_zones(conn, kolkata(9, 0)) == 0
    assert len(queued(conn, "match.plan")) == 1


def test_a_day_whose_03_00_was_missed_is_planned_when_the_worker_is_back(conn):
    person(conn)

    assert jobs.plan_zones(conn, kolkata(9, 0)) == 1  # the worker was down at 03:00

    assert [row.unique_key for row in queued(conn, "match.plan")] == ["match.plan:Asia/Kolkata:2026-11-23"]


def test_each_zone_is_planned_for_its_own_local_date_and_only_people_who_can_be_matched_count(conn):
    person(conn)  # Asia/Kolkata: 03:30 on the 23rd
    person(conn, timezone="Europe/Berlin")  # 23:00 on the 22nd, long after 03:00 of that date
    person(conn, timezone="America/New_York", frequency="paused")  # paused: no zone of their own
    make_user(conn, timezone="Asia/Tokyo")  # no confirmed record: nothing to match
    make_user(conn, timezone="Not/AZone")  # a name this machine cannot read is skipped, not fatal
    conn.execute(text("UPDATE users SET record_confirmed_at = now() WHERE timezone = 'Not/AZone'"))

    assert jobs.plan_zones(conn, kolkata(3, 30)) == 2

    assert [row.unique_key for row in queued(conn, "match.plan")] == [
        "match.plan:Asia/Kolkata:2026-11-23",
        "match.plan:Europe/Berlin:2026-11-22",
    ]


# --- match.plan ----------------------------------------------------------------------------------------------------


def test_the_plan_queues_a_nightly_run_for_each_person_who_is_due_and_no_one_else(conn):
    jobs_changed = datetime(2026, 11, 20, tzinfo=timezone.utc)
    posting = make_job(
        conn, title="Role A", description="Role A. You need Python.", requirements=[("Python", "required")]
    )
    conn.execute(text("UPDATE jobs SET materially_changed_at = :t"), {"t": jobs_changed})

    def ran(user, *, started, local_date=PREVIOUS_DAY):
        run = make_run(conn, user, local_date=local_date)
        conn.execute(text("UPDATE match_runs SET started_at = :t WHERE id = :r"), {"t": started, "r": run})
        return run

    after_the_jobs, before_the_jobs = jobs_changed + timedelta(days=2), jobs_changed - timedelta(days=1)
    never_run = person(conn)
    record_changed = person(conn)
    ran(record_changed, started=after_the_jobs)
    conn.execute(text("UPDATE users SET record_version = 2 WHERE id = :u"), {"u": record_changed})
    jobs_changed_since = person(conn)
    ran(jobs_changed_since, started=before_the_jobs)
    cap_held_back = person(conn)
    held = ran(cap_held_back, started=after_the_jobs)
    make_decision(
        conn, held, cap_held_back, posting, outcome="withheld", reasons=("below_daily_cap",), local_date=PREVIOUS_DAY
    )
    quiet = person(conn)  # the last run is newer than the record and than every job: nothing new
    ran(quiet, started=after_the_jobs)
    already_ran = person(conn)
    ran(already_ran, started=after_the_jobs, local_date=DAY)
    weekly_today = person(conn, frequency="weekly", weekday=DAY.isoweekday())
    weekly_another_day = person(conn, frequency="weekly", weekday=DAY.isoweekday() % 7 + 1)
    paused = person(conn, frequency="paused")
    unconfirmed = make_user(conn)
    elsewhere = person(conn, timezone="Europe/Berlin")

    planned = jobs.plan_zone(conn, "Asia/Kolkata", DAY, kolkata(3, 10))

    due = [never_run, record_changed, jobs_changed_since, cap_held_back, weekly_today]
    assert planned == len(due)
    assert [row.unique_key for row in queued(conn, "match.nightly")] == sorted(
        f"match.nightly:{u}:2026-11-23" for u in due
    )
    for row in queued(conn, "match.nightly"):
        assert (row.queue, row.payload, row.lease_seconds) == (
            "llm",
            {"user_id": str(person_of(row)), "local_date": "2026-11-23"},
            jobs.RUN_LEASE_SECONDS,
        )
    not_due = {quiet, already_ran, weekly_another_day, paused, unconfirmed, elsewhere}
    assert not_due.isdisjoint({person_of(row) for row in queued(conn, "match.nightly")})
    assert jobs.plan_zone(conn, "Asia/Kolkata", DAY, kolkata(3, 11)) == 0  # the same plan again adds nothing


def test_the_plan_waits_while_the_llm_queue_is_behind(conn):
    person(conn)
    now = kolkata(3, 10)
    enqueue(conn, "x.slow", {}, unique_key="x.slow:1", queue="llm", run_at=now - timedelta(minutes=25))
    enqueue(conn, "x.old", {}, unique_key="x.old:1", queue="default", run_at=now - timedelta(hours=5))

    with pytest.raises(jobs.PacingWait):
        jobs.plan_zone(conn, "Asia/Kolkata", DAY, now)
    assert queued(conn, "match.nightly") == []

    conn.execute(
        text("UPDATE queue_jobs SET run_at = :t WHERE unique_key = 'x.slow:1'"), {"t": now - timedelta(minutes=10)}
    )
    assert jobs.plan_zone(conn, "Asia/Kolkata", DAY, now) == 1  # ten minutes behind is within the limit


# --- match.onboarding_sweep ----------------------------------------------------------------------------------------


def test_the_onboarding_sweep_queues_a_first_run_once_for_a_confirmed_person_with_no_run(conn):
    new = person(conn)
    has_run = person(conn)
    make_run(conn, has_run)
    make_user(conn)  # nothing confirmed yet
    suspended = person(conn)
    conn.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": suspended})

    assert jobs.sweep_onboarding(conn) == 1

    (first,) = queued(conn, "match.run")
    assert (first.unique_key, first.queue, first.payload, first.lease_seconds) == (
        f"match.onboarding:{new}",
        "interactive",
        {"user_id": str(new), "trigger": "onboarding"},
        jobs.RUN_LEASE_SECONDS,
    )
    assert jobs.sweep_onboarding(conn) == 0  # never twice
    assert len(queued(conn, "match.run")) == 1


# --- registration and failure --------------------------------------------------------------------------------------


def test_every_handler_is_registered_and_the_two_sweeps_run_every_minute():
    assert scheduler._every["match.zones"] == 60 and scheduler._every["match.onboarding_sweep"] == 60
    handlers = {kind: queue._handlers[kind] for kind in ("match.zones", "match.plan", "match.nightly", "match.run")}
    assert handlers == {
        "match.zones": jobs.zones,
        "match.plan": jobs.plan,
        "match.nightly": jobs.nightly,
        "match.run": jobs.run_now,
    }
    assert queue._handlers["match.onboarding_sweep"] is jobs.onboarding_sweep


def test_the_scheduled_handlers_run_on_an_empty_database(conn):
    jobs.zones(conn, {})
    jobs.onboarding_sweep(conn, {})
    jobs.plan(conn, {"zone": "Asia/Kolkata", "local_date": "2026-11-23"})
    assert queued(conn, "match.nightly") == []


def test_the_run_handlers_pass_the_trigger_and_the_date_to_the_run(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(
        service, "run", lambda user, *, trigger, local_date=None: calls.append((user, trigger, local_date))
    )
    user = uuid4()

    jobs.nightly(conn, {"user_id": str(user), "local_date": "2026-11-23"})
    jobs.run_now(conn, {"user_id": str(user), "trigger": "onboarding"})

    assert calls == [(user, "nightly", DAY), (user, "onboarding", None)]


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [
        (ProviderUnavailable(), ProviderUnavailable),  # the provider is down: retry with the queue's backoff
        (OperationalError("a statement", {}, Exception("connection lost")), OperationalError),  # a database hiccup too
        (service.NotRunnable("gone"), None),  # suspended or deleted since the job was queued: nothing to do
        (RuntimeError("invented bug"), NonRetryable),  # retrying the same input cannot help: dead at once
        (ValueError("bad trigger"), NonRetryable),
    ],
)
def test_the_run_handlers_fail_the_queue_the_right_way(conn, monkeypatch, raised, outcome):
    def fail(user, *, trigger, local_date=None):
        raise raised

    monkeypatch.setattr(service, "run", fail)
    payload = {"user_id": str(uuid4()), "local_date": "2026-11-23"}

    if outcome is None:
        assert jobs.nightly(conn, payload) is None
    else:
        with pytest.raises(outcome):
            jobs.nightly(conn, payload)
