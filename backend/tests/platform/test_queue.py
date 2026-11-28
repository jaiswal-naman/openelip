"""PLT-06: the queue keeps its promises. Each behaviour of plan 01-02 task 1 is one test.

The module gets a database of its own (named for this process, migrated, dropped at the end of the session) and talks to
it as the real app_worker role, so the grants the worker needs are exercised too. A private database also means the
other test modules' table clean-up cannot reach the rows these tests commit.
"""

import json
import logging
import os
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.platform import queue


class InvalidPayload(queue.NonRetryable):
    pass


@queue.handler("test.noop")
def _noop(conn, payload):
    pass


@queue.handler("test.boom")
def _boom(conn, payload):
    raise RuntimeError("alice@example.test pasted her resume here")


@queue.handler("test.invalid")
def _invalid(conn, payload):
    raise InvalidPayload("the payload is wrong")


@queue.handler("test.write_then_fail")
def _write_then_fail(conn, payload):
    queue.enqueue(conn, "test.noop", {}, unique_key="ghost")
    raise RuntimeError("after writing")


@queue.handler("test.write")
def _write(conn, payload):
    queue.enqueue(conn, "test.noop", {}, unique_key="child")


@pytest.fixture(scope="module")
def worker_engine(make_database, alembic_run):
    url = make_database(f"app_queue_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    engine = create_engine(
        make_url(os.environ["DATABASE_URL_WORKER"]).set(database=url.database, drivername="postgresql+psycopg")
    )
    yield engine
    engine.dispose()


@pytest.fixture
def factory(worker_engine):
    """A connection factory like db.transaction: the block commits if it ends normally and rolls back if it raises."""
    with worker_engine.begin() as conn:
        conn.execute(text("DELETE FROM queue_jobs"))

    @contextmanager
    def connection():
        with worker_engine.connect() as conn:
            yield conn
            conn.commit()

    return connection


def add(factory, kind, key, **options):
    with factory() as conn:
        return queue.enqueue(conn, kind, {}, unique_key=key, **options)


def job(factory, job_id):
    with factory() as conn:
        return conn.execute(
            text("SELECT *, extract(epoch FROM run_at - now()) AS wait FROM queue_jobs WHERE id = :id"), {"id": job_id}
        ).one()


def count(factory, key):
    with factory() as conn:
        return conn.execute(text("SELECT count(*) FROM queue_jobs WHERE unique_key = :k"), {"k": key}).scalar_one()


def set_times(factory, job_id, **intervals):
    """Move a job's timestamps into the past, for example locked_at="1 hour"."""
    with factory() as conn:
        for column, interval in intervals.items():
            conn.execute(
                text(f"UPDATE queue_jobs SET {column} = now() - CAST(:i AS interval) WHERE id = :id"),
                {"i": interval, "id": job_id},
            )


def test_a_repeated_unique_key_makes_one_job_even_after_the_first_one_succeeded(factory):
    first = add(factory, "test.noop", "once")
    assert isinstance(first, int) and add(factory, "test.noop", "once") is None
    assert queue.run_once(factory, "w1") == 1 and job(factory, first).status == "succeeded"
    assert add(factory, "test.noop", "once") is None
    assert count(factory, "once") == 1


def test_a_job_made_for_a_person_carries_their_id_and_one_made_for_no_one_does_not(factory):
    """queue_jobs.user_id is what the export reads and the erasure cascades on (TRU-01, TRU-02)."""
    with factory() as conn:
        user = conn.execute(
            text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": f"queue-{os.getpid()}@example.test"}
        ).scalar_one()
    mine = add(factory, "test.noop", "mine", user_id=user)
    nobody = add(factory, "test.noop", "nobody")
    assert (job(factory, mine).user_id, job(factory, nobody).user_id) == (user, None)


def test_a_job_enqueued_in_a_transaction_that_rolls_back_never_exists(factory):
    with pytest.raises(RuntimeError):
        with factory() as conn:
            queue.enqueue(conn, "test.noop", {}, unique_key="rolled-back")
            raise RuntimeError("the caller's own change failed")
    assert count(factory, "rolled-back") == 0
    assert add(factory, "test.noop", "rolled-back") is not None


def test_a_failing_job_retries_with_backoff_and_is_dead_after_max_attempts(factory):
    job_id = add(factory, "test.boom", "boom", max_attempts=3)
    for attempt, window in enumerate([(29, 34), (119, 133)], start=1):
        assert queue.run_once(factory, "w1") == 1
        row = job(factory, job_id)
        assert (row.status, row.attempts) == ("queued", attempt)
        assert window[0] <= row.wait <= window[1], row.wait  # 30 s then 2 min, plus up to 10% jitter
        assert queue.run_once(factory, "w1") == 0  # not due yet
        set_times(factory, job_id, run_at="1 second")
    assert queue.run_once(factory, "w1") == 1
    row = job(factory, job_id)
    assert (row.status, row.attempts, row.last_error_code) == ("dead", 3, "RuntimeError")
    assert row.finished_at is not None


def test_a_failure_is_stored_and_logged_by_class_name_never_by_message(factory, caplog):
    job_id = add(factory, "test.boom", "boom")
    with caplog.at_level(logging.INFO, logger="app.event"):
        queue.run_once(factory, "w1")
    with factory() as conn:
        whole_row = conn.execute(text("SELECT q::text FROM queue_jobs q WHERE id = :id"), {"id": job_id}).scalar_one()
    assert "alice" not in whole_row and "resume" not in whole_row
    lines = [json.loads(record.getMessage()) for record in caplog.records if record.name == "app.event"]
    assert [line["status"] for line in lines] == ["running", "queued"]
    assert "alice" not in "".join(record.getMessage() for record in caplog.records)
    allowed = {"ts", "level", "event", "job_id", "kind", "queue", "status", "attempts", "duration_ms", "error_code"}
    assert all(set(line) <= allowed and line["event"] == "queue.job" for line in lines)
    assert lines[1]["error_code"] == "RuntimeError" and lines[1]["duration_ms"] >= 0


def test_a_non_retryable_error_goes_dead_at_once(factory):
    job_id = add(factory, "test.invalid", "invalid")
    assert queue.run_once(factory, "w1") == 1
    row = job(factory, job_id)
    assert (row.status, row.attempts, row.last_error_code) == ("dead", 1, "InvalidPayload")


def test_an_unknown_kind_goes_dead_with_unknown_kind(factory):
    job_id = add(factory, "test.nobody_handles_this", "orphan")
    assert queue.run_once(factory, "w1") == 1
    row = job(factory, job_id)
    assert (row.status, row.last_error_code) == ("dead", "unknown_kind")


def test_two_claims_never_take_the_same_job(factory):
    add(factory, "test.noop", "one")
    add(factory, "test.noop", "two")
    with factory() as a, factory() as b, factory() as c:  # a's claim is still uncommitted while b and c ask
        first = queue.claim(a, "wa")
        second = queue.claim(b, "wb")  # SKIP LOCKED: b is not blocked by a's lock, it takes the other job
        third = queue.claim(c, "wc")
    assert first and second and first.id != second.id
    assert third is None


def test_a_job_that_waits_for_its_run_at_is_not_claimed(factory):
    with factory() as conn:
        queue.enqueue(
            conn,
            "test.noop",
            {},
            unique_key="later",
            run_at=conn.execute(text("SELECT now() + interval '1 hour'")).scalar_one(),
        )
    assert queue.run_once(factory, "w1") == 0


def test_one_worker_serving_every_queue_takes_the_persons_work_before_older_background_jobs(factory):
    add(factory, "test.noop", "fetch", queue="ingest")
    add(factory, "test.noop", "export")  # the default queue, enqueued after the fetch
    add(factory, "test.noop", "parse", queue="interactive")
    with factory() as conn:
        order = [queue.claim(conn, "w1").queue for _ in range(3)]
    assert order == ["interactive", "default", "ingest"]


def test_a_worker_takes_only_the_queues_it_was_given(factory):
    add(factory, "test.noop", "a-job", queue="alpha")
    other = add(factory, "test.noop", "b-job", queue="beta")
    assert queue.run_once(factory, "w1", ("beta",)) == 1
    assert job(factory, other).status == "succeeded"
    assert queue.run_once(factory, "w1", ("beta",)) == 0
    assert queue.run_once(factory, "w1") == 1  # no list: every queue


def test_a_lease_that_ran_out_returns_the_job_to_queued(factory):
    job_id = add(factory, "test.noop", "lease", max_attempts=3)
    with factory() as conn:
        assert queue.claim(conn, "worker-that-died") is not None
    with factory() as conn:
        assert queue.reap(conn) == 0  # the lease (300 s) is still running
    set_times(factory, job_id, locked_at="1 hour")
    with factory() as conn:
        assert queue.reap(conn) == 1
    row = job(factory, job_id)
    assert (row.status, row.locked_by, row.last_error_code, row.attempts) == ("queued", None, "lease_expired", 1)
    assert queue.run_once(factory, "w2") == 1 and job(factory, job_id).status == "succeeded"


def test_a_job_that_kills_its_worker_on_its_last_attempt_is_dead_not_requeued(factory):
    job_id = add(factory, "test.noop", "poison", max_attempts=1)
    with factory() as conn:
        queue.claim(conn, "worker-that-died")
    set_times(factory, job_id, locked_at="1 hour")
    with factory() as conn:
        assert queue.reap(conn) == 1
    assert job(factory, job_id).status == "dead"


def test_the_handler_and_the_succeeded_mark_commit_together_and_a_failure_undoes_the_handler(factory):
    add(factory, "test.write_then_fail", "fails")
    add(factory, "test.write", "works")
    queue.run_once(factory, "w1")
    queue.run_once(factory, "w1")
    assert count(factory, "ghost") == 0  # written by the handler that then raised: rolled back
    assert count(factory, "child") == 1


def test_prune_removes_succeeded_jobs_after_7_days_and_dead_ones_after_30(factory):
    ids = {
        "old-ok": add(factory, "test.noop", "old-ok"),
        "new-ok": add(factory, "test.noop", "new-ok"),
        "old-dead": add(factory, "test.noop", "old-dead"),
        "new-dead": add(factory, "test.noop", "new-dead"),
        "waiting": add(factory, "test.noop", "waiting"),
    }
    for key, status, age in [
        ("old-ok", "succeeded", "8 days"),
        ("new-ok", "succeeded", "6 days"),
        ("old-dead", "dead", "31 days"),
        ("new-dead", "dead", "29 days"),
    ]:
        with factory() as conn:
            conn.execute(
                text("UPDATE queue_jobs SET status = CAST(:s AS queue_status) WHERE id = :id"),
                {"s": status, "id": ids[key]},
            )
        set_times(factory, ids[key], finished_at=age)
    set_times(factory, ids["waiting"], created_at="90 days")
    with factory() as conn:
        assert queue.prune(conn) == 2
    assert [count(factory, key) for key in ids] == [0, 1, 0, 1, 1]


def test_registering_two_handlers_for_one_kind_is_refused():
    with pytest.raises(ValueError):
        queue.handler("test.noop")(lambda conn, payload: None)
