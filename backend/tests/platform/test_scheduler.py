"""PLT-06: a scheduled job fires once in its slot and never twice, even with two schedulers ticking.

Like test_queue.py this module has a database of its own and uses the real app_worker role. The clock is a plain
datetime passed to tick(), so no test waits for time to pass.
"""

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.platform import queue, scheduler

RAN: list[str] = []
NAME = "test.sched.every_minute"
UPKEEP = {
    name: seconds for name, seconds in scheduler._every.items() if name.startswith("queue.")
}  # before any patching


@queue.handler(NAME)
def _every_minute(conn, payload):
    RAN.append("ran")


@pytest.fixture(scope="module")
def worker_engine(make_database, alembic_run):
    url = make_database(f"app_sched_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    engine = create_engine(
        make_url(os.environ["DATABASE_URL_WORKER"]).set(database=url.database, drivername="postgresql+psycopg")
    )
    yield engine
    engine.dispose()


@pytest.fixture
def factory(worker_engine, monkeypatch):
    """A db.transaction-like factory, an empty queue, and a schedule holding only the one-minute test entry."""
    with worker_engine.begin() as conn:
        conn.execute(text("DELETE FROM queue_jobs"))
    monkeypatch.setattr(scheduler, "_every", {NAME: 60})
    RAN.clear()

    @contextmanager
    def connection():
        with worker_engine.connect() as conn:
            yield conn
            conn.commit()

    return connection


def tick(factory, now):
    with factory() as conn:
        return scheduler.tick(conn, now)


def keys(factory):
    with factory() as conn:
        return conn.execute(text("SELECT unique_key FROM queue_jobs ORDER BY id")).scalars().all()


def epoch(moment):
    return int(moment.timestamp())


def test_a_job_registered_every_60_seconds_is_enqueued_with_its_slot_in_the_key(factory):
    now = datetime(2030, 1, 1, 10, 0, 30, tzinfo=timezone.utc)
    assert tick(factory, now) == 1
    assert keys(factory) == [f"{NAME}:{epoch(datetime(2030, 1, 1, 10, 0, 0, tzinfo=timezone.utc))}"]  # the slot's start


def test_two_schedulers_ticking_in_the_same_slot_enqueue_once(factory):
    now = datetime(2030, 1, 1, 10, 0, 30, tzinfo=timezone.utc)
    assert tick(factory, now) == 1  # one scheduler (one connection) ...
    assert tick(factory, now + timedelta(seconds=29)) == 0  # ... and another, still inside the same minute
    assert len(keys(factory)) == 1


def test_the_next_slot_is_enqueued_one_period_later_and_the_queue_runs_both(factory):
    start = datetime(2030, 1, 1, 10, 0, 30, tzinfo=timezone.utc)
    assert tick(factory, start) == 1
    assert tick(factory, start + timedelta(seconds=60)) == 1  # 10:01:30, the next slot
    assert len(keys(factory)) == 2
    assert [queue.run_once(factory, "w1") for _ in range(3)] == [1, 1, 0]
    assert RAN == ["ran", "ran"]


def test_a_daily_entry_has_one_slot_per_utc_day(factory, monkeypatch):
    monkeypatch.setattr(scheduler, "_every", {NAME: 86400})
    assert tick(factory, datetime(2030, 1, 1, 0, 0, 1, tzinfo=timezone.utc)) == 1
    assert tick(factory, datetime(2030, 1, 1, 23, 59, 59, tzinfo=timezone.utc)) == 0
    assert tick(factory, datetime(2030, 1, 2, 0, 0, 1, tzinfo=timezone.utc)) == 1


def test_every_registers_the_schedule_and_the_job_handler_together():
    def hourly(conn, payload):
        pass

    try:
        assert scheduler.every("test.sched.registered", 3600)(hourly) is hourly
        assert scheduler._every["test.sched.registered"] == 3600
        assert queue._handlers["test.sched.registered"] is hourly
        with pytest.raises(ValueError):
            scheduler.every("test.sched.registered", 60)(hourly)
    finally:
        scheduler._every.pop("test.sched.registered", None)


def test_the_queues_own_upkeep_is_scheduled_and_actually_runs(factory, monkeypatch):
    assert UPKEEP == {"queue.reap": 60, "queue.prune": 86400}
    monkeypatch.setattr(scheduler, "_every", UPKEEP)
    with factory() as conn:
        queue.enqueue(conn, "test.sched.every_minute", {}, unique_key="stuck")
        queue.claim(conn, "worker-that-died")
        conn.execute(text("UPDATE queue_jobs SET locked_at = now() - interval '1 hour' WHERE unique_key = 'stuck'"))
    assert tick(factory, datetime.now(timezone.utc)) == 2  # the reaper and the pruner
    while queue.run_once(factory, "w1"):
        pass
    with factory() as conn:
        row = conn.execute(text("SELECT status, attempts FROM queue_jobs WHERE unique_key = 'stuck'")).one()
    assert tuple(row) == ("succeeded", 2)  # the reaper gave the dead worker's job back, and it then ran
