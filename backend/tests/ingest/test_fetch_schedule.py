"""IDX-07: every active source is fetched once per six-hour slot with nobody running a command; a repeat adds nothing.

Almost everything runs in the `conn` fixture's transaction, which is rolled back. The clock is a datetime passed in, so
no test waits for time to pass. The slot is the number of whole six-hour periods since 1970, which puts its edges at
00:00, 06:00, 12:00 and 18:00 UTC. The last test runs the refresh as the worker's own database login instead, to prove
that role holds every privilege the new columns need.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.ingest import jobs
from app.platform import db, queue, scheduler

SIX_HOURS = 6 * 3600
NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)  # inside the 06:00 to 12:00 slot
SLOT = int(NOW.timestamp()) // SIX_HOURS


def add_source(conn, token, *, active=True):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name, is_active) VALUES ('lever', :t, :t, :a) RETURNING id"),
        {"t": token, "a": active},
    ).scalar_one()


def queued(conn, kind):
    rows = conn.execute(
        text("SELECT unique_key, queue, payload FROM queue_jobs WHERE kind = :k ORDER BY id"), {"k": kind}
    ).all()
    return [(row.unique_key, row.queue, row.payload) for row in rows]


def test_every_active_source_gets_one_fetch_with_its_slot_in_the_key(conn):
    first, second = add_source(conn, "first"), add_source(conn, "second")
    add_source(conn, "retired", active=False)

    assert jobs.queue_fetches(conn, NOW) == 2

    assert queued(conn, "ingest.fetch") == [
        (f"ingest.fetch:{first}:{SLOT}", "ingest", {"source_id": first, "slot": SLOT}),
        (f"ingest.fetch:{second}:{SLOT}", "ingest", {"source_id": second, "slot": SLOT}),
    ]
    assert queued(conn, "ingest.discover") == []


def test_a_repeat_in_the_same_slot_adds_nothing_and_the_next_slot_queues_again(conn):
    add_source(conn, "first")
    add_source(conn, "second")
    assert jobs.queue_fetches(conn, NOW) == 2

    assert jobs.queue_fetches(conn, NOW) == 0  # the same moment, from another worker
    assert (
        jobs.queue_fetches(conn, datetime(2030, 1, 1, 11, 59, 59, tzinfo=timezone.utc)) == 0
    )  # last second of the slot
    assert len(queued(conn, "ingest.fetch")) == 2

    assert jobs.queue_fetches(conn, datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)) == 2  # the next slot
    assert len(queued(conn, "ingest.fetch")) == 4


def test_a_source_added_during_a_slot_is_fetched_in_that_slot_without_repeating_the_others(conn):
    add_source(conn, "first")
    assert jobs.queue_fetches(conn, NOW) == 1
    add_source(conn, "second")
    assert jobs.queue_fetches(conn, NOW + timedelta(minutes=5)) == 1
    assert len(queued(conn, "ingest.fetch")) == 2


def test_with_no_active_source_discovery_is_queued_once_per_slot_instead(conn):
    add_source(conn, "retired", active=False)

    assert jobs.queue_fetches(conn, NOW) == 0
    assert jobs.queue_fetches(conn, NOW) == 0

    assert queued(conn, "ingest.fetch") == []
    assert queued(conn, "ingest.discover") == [(f"ingest.discover:{SLOT}", "ingest", {})]


def test_discovery_fetches_the_boards_it_registers_at_once_instead_of_waiting_for_the_next_slot(conn, monkeypatch):
    # A new install starts with no board: the jobs must not wait up to six hours for the next sweep to appear.
    async def live(tokens):
        return [("lever", "found", "Found Co", 3)]

    monkeypatch.setattr(jobs.discover, "candidate_tokens", lambda: ["found"])
    monkeypatch.setattr(jobs.discover, "find_live_boards", live)

    jobs.discover_sources(conn, {})

    source_id = conn.execute(text("SELECT id FROM job_sources WHERE token = 'found'")).scalar_one()
    assert [(key.split(":")[1], payload["source_id"]) for key, _, payload in queued(conn, "ingest.fetch")] == [
        (str(source_id), source_id)
    ]


def test_discovery_that_finds_no_board_queues_nothing(conn, monkeypatch):
    async def none(tokens):
        return []

    monkeypatch.setattr(jobs.discover, "candidate_tokens", lambda: ["gone"])
    monkeypatch.setattr(jobs.discover, "find_live_boards", none)

    jobs.discover_sources(conn, {})

    assert queued(conn, "ingest.fetch") == [] and queued(conn, "ingest.discover") == []


def test_the_schedule_queues_the_sweep_on_its_own_and_the_sweep_queues_the_fetches(conn):
    assert scheduler._every["ingest.fetch_sweep"] == SIX_HOURS
    assert queue._handlers["ingest.fetch_sweep"] is jobs.fetch_sweep
    source = add_source(conn, "first")

    scheduler.tick(conn, NOW)  # what the worker loop does about once a second
    scheduler.tick(conn, NOW + timedelta(hours=1))  # still inside the same period
    sweeps = queued(conn, "ingest.fetch_sweep")
    assert [key for key, _, _ in sweeps] == [
        f"ingest.fetch_sweep:{int(datetime(2030, 1, 1, 6, tzinfo=timezone.utc).timestamp())}"
    ]

    # The worker would claim that job and run its handler, which reads the clock itself.
    slots = {int(time.time()) // SIX_HOURS}
    jobs.fetch_sweep(conn, sweeps[0][2])
    slots.add(int(time.time()) // SIX_HOURS)  # the two differ only if the slot turned over during this test
    keys = {key for key, _, _ in queued(conn, "ingest.fetch")}
    assert len(keys) == 1 and keys <= {f"ingest.fetch:{source}:{slot}" for slot in slots}


def test_a_fetch_queued_by_the_sweep_runs_through_the_phase_1_handler(conn, monkeypatch):
    source = add_source(conn, "first")
    jobs.queue_fetches(conn, NOW)
    payload = queued(conn, "ingest.fetch")[0][2]

    async def empty_feed(ats, token):
        return "ok", 200, [], None

    monkeypatch.setattr(jobs, "fetch_feed", empty_feed)
    queue._handlers["ingest.fetch"](conn, payload)

    assert conn.execute(
        text("SELECT status::text FROM fetch_runs WHERE source_id = :s"), {"s": source}
    ).scalars().all() == ["ok"]
    assert [key for key, _, _ in queued(conn, "ingest.dedupe")] == [f"ingest.dedupe:{SLOT}"]


@pytest.fixture
def committed_index(engine):
    """The worker's own login commits, so this test cleans up the way the API tests do: empty before and after."""
    empty = text("TRUNCATE queue_jobs, fetch_runs, jobs, job_sources RESTART IDENTITY CASCADE")
    with engine.begin() as c:
        c.execute(empty)
    yield
    with engine.begin() as c:
        c.execute(empty)


def feed_posting(n, description):
    return {
        "external_id": f"worker-{n}",
        "title": f"Worker engineer {n}",
        "location_raw": "Pune, India",
        "country_hint": None,
        "remote_hint": None,
        "employment_type": "Full-time",
        "department": None,
        "description": description,
        "apply_url": f"https://example.test/jobs/{n}",
        "posted_at": None,
    }


def test_the_worker_role_holds_every_privilege_the_refresh_needs(engine, committed_index, monkeypatch):
    with engine.begin() as c:
        source = add_source(c, "first")
    text_one = "Invented posting one, long enough to be stored by the index."
    text_two = "Invented posting two, long enough to be stored by the index."
    feeds = iter(
        [
            ("ok", 200, [feed_posting(1, text_one), feed_posting(2, text_two)], None),  # two new jobs
            ("ok", 200, [feed_posting(1, text_one), feed_posting(2, text_two + " Rewritten.")], None),  # one changed
            ("http_error", 503, None, None),  # a failed fetch
        ]
    )

    async def next_feed(ats, token):
        return next(feeds)

    monkeypatch.setattr(jobs, "fetch_feed", next_feed)

    with db.transaction(role="worker") as conn:
        assert jobs.queue_fetches(conn, NOW) == 1
    for _ in range(3):
        with db.transaction(role="worker") as conn:
            jobs.fetch_source(conn, {"source_id": source, "slot": SLOT})

    with engine.connect() as c:
        runs = c.execute(text("SELECT status::text, jobs_new, jobs_changed FROM fetch_runs ORDER BY id")).all()
        versions = c.execute(text("SELECT content_version FROM jobs ORDER BY external_id")).scalars().all()
        failures = c.execute(text("SELECT consecutive_failures FROM job_sources")).scalar_one()
    assert [tuple(run) for run in runs] == [("ok", 2, 0), ("ok", 0, 1), ("http_error", 0, 0)]
    assert versions == [1, 2]
    assert failures == 1
