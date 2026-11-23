"""BRF-01 and BRF-03: who is due for a brief and when (the per-minute brief.sweep).

Rows are invented (tests/match/factories.py) on the `conn` fixture's connection, which is rolled back. The sweep is
given the moment it runs at, so every minute of a morning can be tried: 05:29, 05:31, 06:16.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief import jobs, mail
from app.brief.api import compose
from app.match.queries import nightly_key
from app.platform import queue, relay, scheduler
from tests.match.factories import LOCAL_DATE, make_record, make_run, make_user

IST = ZoneInfo("Asia/Kolkata")
CONFIRMED = datetime(2026, 11, 20, 12, 0, tzinfo=IST)  # well before the morning under test


def at(hour, minute, day=LOCAL_DATE):
    """The moment it is hour:minute on that day in Asia/Kolkata."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def person(conn, **user):
    """A person who can get a brief: an active account with a record confirmed days ago."""
    who = make_user(conn, **user)
    make_record(conn, who)
    conn.execute(text("UPDATE users SET record_confirmed_at = :t WHERE id = :u"), {"t": CONFIRMED, "u": who})
    return who


def queued(conn):
    """The brief.compose jobs the sweep has queued, as (user, local date) pairs."""
    rows = conn.execute(
        text("SELECT payload FROM queue_jobs WHERE kind = 'brief.compose' ORDER BY id"),
    ).scalars()
    return [(p["user_id"], p["local_date"]) for p in rows]


def start_nightly_job(conn, user):
    """A nightly run asked for and not started: its queue job is waiting."""
    queue.enqueue(conn, "match.nightly", {"user_id": str(user)}, unique_key=nightly_key(user, LOCAL_DATE), queue="llm")


# ---- the clock -------------------------------------------------------------------------------------------------


def test_nothing_is_queued_before_0530_one_job_after_it_and_a_second_tick_adds_nothing(conn):
    ada = person(conn)

    assert jobs.queue_composes(conn, at(5, 29)) == 0 and queued(conn) == []
    assert jobs.queue_composes(conn, at(5, 31)) == 1
    assert queued(conn) == [(str(ada), "2026-11-23")]
    assert jobs.queue_composes(conn, at(5, 32)) == 0 and jobs.queue_composes(conn, at(5, 31)) == 0
    assert len(queued(conn)) == 1
    key = conn.execute(text("SELECT unique_key FROM queue_jobs WHERE kind = 'brief.compose'")).scalar_one()
    assert key == f"brief.compose:{ada}:2026-11-23"


def test_the_compose_job_names_its_person_so_the_export_lists_it_and_erasing_them_deletes_it(conn):
    ada = person(conn)

    assert jobs.queue_composes(conn, at(5, 31)) == 1

    owner = conn.execute(text("SELECT user_id FROM queue_jobs WHERE kind = 'brief.compose'")).scalar_one()
    assert owner == ada


def test_a_nightly_run_that_is_not_done_is_waited_for_until_0615_then_the_brief_goes_without_it(conn):
    queued_run, running_run = person(conn), person(conn)
    start_nightly_job(conn, queued_run)  # its job is waiting in the queue
    make_run(conn, running_run, status="running")

    assert jobs.queue_composes(conn, at(5, 31)) == 0
    assert jobs.queue_composes(conn, at(6, 14)) == 0
    assert jobs.queue_composes(conn, at(6, 16)) == 2
    assert {user for user, _ in queued(conn)} == {str(queued_run), str(running_run)}


def test_a_nightly_run_that_is_done_or_failed_or_never_asked_for_is_not_waited_for(conn):
    done, failed, none = person(conn), person(conn), person(conn)
    make_run(conn, done)
    make_run(conn, failed, status="failed")

    assert jobs.queue_composes(conn, at(5, 31)) == 3
    assert {user for user, _ in queued(conn)} == {str(done), str(failed), str(none)}


# ---- who is due ------------------------------------------------------------------------------------------------


def test_a_weekly_person_is_due_only_on_their_day(conn):
    today = person(conn, frequency="weekly", weekday=LOCAL_DATE.isoweekday())
    person(conn, frequency="weekly", weekday=LOCAL_DATE.isoweekday() % 7 + 1)  # another day of the week

    assert jobs.queue_composes(conn, at(5, 31)) == 1
    assert queued(conn) == [(str(today), "2026-11-23")]
    assert jobs.queue_composes(conn, at(5, 31, LOCAL_DATE + timedelta(days=1))) == 1  # tomorrow it is the other's day


def test_a_paused_person_an_unconfirmed_record_and_a_suspended_account_are_never_due(conn):
    person(conn, frequency="paused")
    unconfirmed = make_user(conn)
    make_record(conn, unconfirmed)
    conn.execute(text("UPDATE users SET record_confirmed_at = NULL WHERE id = :u"), {"u": unconfirmed})
    suspended = person(conn)
    conn.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": suspended})

    assert jobs.queue_composes(conn, at(5, 31)) == 0 and jobs.queue_composes(conn, at(23, 0)) == 0


def test_a_person_who_already_has_the_days_brief_is_not_due_but_yesterdays_does_not_count(conn):
    ada, bob = person(conn), person(conn)
    compose(conn, ada, LOCAL_DATE, now=at(5, 20))
    compose(conn, bob, LOCAL_DATE - timedelta(days=1), now=at(5, 20, LOCAL_DATE - timedelta(days=1)))

    assert jobs.queue_composes(conn, at(5, 31)) == 1
    assert queued(conn) == [(str(bob), "2026-11-23")]


def test_someone_who_confirmed_their_record_after_the_morning_starts_gets_their_first_brief_tomorrow(conn):
    """The onboarding page promises "Your first brief arrives tomorrow by 07:00", and a brief made at 15:00 for
    "this morning" would be one more thing to explain."""
    late = person(conn)
    conn.execute(text("UPDATE users SET record_confirmed_at = :t WHERE id = :u"), {"t": at(9, 0), "u": late})
    early = person(conn)
    conn.execute(text("UPDATE users SET record_confirmed_at = :t WHERE id = :u"), {"t": at(5, 0), "u": early})

    assert jobs.queue_composes(conn, at(15, 0)) == 1
    assert queued(conn) == [(str(early), "2026-11-23")]
    assert (
        jobs.queue_composes(conn, at(5, 31, LOCAL_DATE + timedelta(days=1))) == 2
    )  # tomorrow: both, one new date each


def test_the_local_date_is_the_persons_own(conn):
    new_york = person(conn, timezone="America/New_York")  # 19:01 on 22 November when it is 05:31 in Kolkata

    assert jobs.queue_composes(conn, at(5, 31)) == 1
    assert queued(conn) == [(str(new_york), "2026-11-22")]


def test_a_time_zone_this_machine_does_not_know_skips_that_person_and_not_the_sweep(conn):
    broken = person(conn)
    conn.execute(text("UPDATE users SET timezone = 'Mars/Olympus_Mons' WHERE id = :u"), {"u": broken})
    ada = person(conn)

    assert jobs.queue_composes(conn, at(5, 31)) == 1
    assert queued(conn) == [(str(ada), "2026-11-23")]


# ---- wiring ----------------------------------------------------------------------------------------------------


def test_the_sweep_runs_every_minute_and_the_compose_job_and_the_email_template_are_registered():
    assert scheduler._every["brief.sweep"] == 60 and queue._handlers["brief.sweep"] is jobs.sweep
    assert queue._handlers["brief.compose"] is jobs.compose_one
    assert relay.RESOLVERS["brief_v1"] is mail.resolve
