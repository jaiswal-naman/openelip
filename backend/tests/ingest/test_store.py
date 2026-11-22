"""The ingest rules survive the move to queue jobs: storing a feed, no closing on a bad fetch, one run per fetch.

Feeds are invented fixtures shaped like the adapters' output; nothing here touches the network. Every test runs in the
`conn` fixture's transaction, which is rolled back.
"""

import asyncio

from sqlalchemy import text

from app.ingest import discover, jobs
from app.ingest.fetch import mark_duplicates, store
from app.platform import queue


def posting(n, description=None, location="Pune, India"):
    return {
        "external_id": f"fixture-{n}",
        "title": f"Fixture engineer {n}",
        "location_raw": location,
        "country_hint": None,
        "remote_hint": None,
        "employment_type": None,
        "department": None,
        "description": description or f"Invented fixture posting number {n}: build and run the fixture services.",
        "apply_url": f"https://example.test/jobs/{n}",
        "posted_at": None,
    }


def new_source(conn):
    return conn.execute(
        text(
            "INSERT INTO job_sources (ats, token, company_name) "
            "VALUES ('greenhouse', 'fixture-board', 'Fixture Co') RETURNING id"
        )
    ).scalar_one()


def open_jobs(conn, source_id):
    return conn.execute(
        text("SELECT count(*) FROM jobs WHERE source_id = :s AND status = 'open'"), {"s": source_id}
    ).scalar_one()


def test_a_feed_is_stored_then_recognised_then_edited_then_closed_when_it_leaves_the_feed(conn):
    source_id = new_source(conn)
    feed = [posting(1), posting(2), posting(3)]

    first = store(conn, source_id, "Fixture Co", feed)
    assert (first["status"], first["seen"], first["new"], first["changed"], first["closed"]) == ("ok", 3, 3, 0, 0)

    again = store(conn, source_id, "Fixture Co", feed)
    assert (again["new"], again["changed"], again["closed"]) == (0, 0, 0)

    feed[1] = posting(2, description="Invented fixture posting number 2, now with an edited description.")
    edited = store(conn, source_id, "Fixture Co", feed)
    assert (edited["new"], edited["changed"]) == (0, 1)
    version = conn.execute(text("SELECT content_version FROM jobs WHERE external_id = 'fixture-2'")).scalar_one()
    assert version == 2

    gone = store(conn, source_id, "Fixture Co", feed[:2])  # posting 3 left its feed
    assert (gone["status"], gone["closed"]) == ("ok", 1)
    assert conn.execute(text("SELECT status FROM jobs WHERE external_id = 'fixture-3'")).scalar_one() == "closed"


def test_an_empty_feed_for_a_busy_source_is_suspicious_and_closes_nothing(conn):
    source_id = new_source(conn)
    store(conn, source_id, "Fixture Co", [posting(n) for n in range(6)])
    result = store(conn, source_id, "Fixture Co", [])
    assert (result["status"], result["closed"]) == ("suspicious_empty", 0)
    assert open_jobs(conn, source_id) == 6


def test_an_unknown_location_stays_null_and_a_posting_without_a_body_is_skipped(conn):
    source_id = new_source(conn)
    result = store(conn, source_id, "Fixture Co", [posting(1, location="HQ"), posting(2, description="too short")])
    assert (result["seen"], result["new"]) == (1, 1)
    row = conn.execute(
        text("SELECT country, city, work_arrangements::text[] FROM jobs WHERE source_id = :s"), {"s": source_id}
    ).one()
    assert tuple(row) == (None, None, [])  # is_remote became work_arrangements: "does not say" is the empty list


def test_postings_with_the_same_company_title_and_place_fold_onto_the_oldest(conn):
    source_id = new_source(conn)
    twin = posting(2)
    twin["title"] = posting(1)["title"]
    store(conn, source_id, "Fixture Co", [posting(1), twin])
    mark_duplicates(conn)
    rows = conn.execute(
        text("SELECT id, canonical_job_id FROM jobs WHERE source_id = :s ORDER BY id"), {"s": source_id}
    ).all()
    assert rows[0].canonical_job_id is None and rows[1].canonical_job_id == rows[0].id


def feed_returning(monkeypatch, result):
    async def fake(ats, token):
        return result

    monkeypatch.setattr(jobs, "fetch_feed", fake)


def test_the_fetch_job_writes_exactly_one_fetch_run_and_queues_one_dedupe_for_the_slot(conn, monkeypatch):
    source_id = new_source(conn)
    feed_returning(monkeypatch, ("ok", 200, [posting(1), posting(2)], None))
    jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-1"})

    runs = conn.execute(
        text("SELECT status, http_status, jobs_seen, jobs_new FROM fetch_runs WHERE source_id = :s"), {"s": source_id}
    ).all()
    assert [tuple(run) for run in runs] == [("ok", 200, 2, 2)]
    assert (
        conn.execute(text("SELECT last_status FROM job_sources WHERE id = :s"), {"s": source_id}).scalar_one() == "ok"
    )
    dedupe = conn.execute(
        text(
            "SELECT queue, run_at > now() + interval '9 minutes' FROM queue_jobs "
            "WHERE unique_key = 'ingest.dedupe:slot-1'"
        )
    ).one()
    assert tuple(dedupe) == ("ingest", True)

    jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-1"})  # a second source in the same slot
    assert (
        conn.execute(text("SELECT count(*) FROM fetch_runs WHERE source_id = :s"), {"s": source_id}).scalar_one() == 2
    )
    assert conn.execute(text("SELECT count(*) FROM queue_jobs WHERE kind = 'ingest.dedupe'")).scalar_one() == 1


def test_a_failed_fetch_is_recorded_and_never_closes_the_jobs_already_stored(conn, monkeypatch):
    source_id = new_source(conn)
    feed_returning(monkeypatch, ("ok", 200, [posting(n) for n in range(6)], None))
    jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-1"})
    for failure in (("http_error", 503, None, None), ("network_error", None, None, "ConnectError")):
        feed_returning(monkeypatch, failure)
        jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-2"})
        assert (
            conn.execute(text("SELECT last_status FROM job_sources WHERE id = :s"), {"s": source_id}).scalar_one()
            == failure[0]
        )
        assert open_jobs(conn, source_id) == 6
    statuses = (
        conn.execute(text("SELECT status FROM fetch_runs WHERE source_id = :s ORDER BY id"), {"s": source_id})
        .scalars()
        .all()
    )
    assert statuses == ["ok", "http_error", "network_error"]


def test_a_source_deactivated_after_the_job_was_queued_is_skipped(conn, monkeypatch):
    source_id = new_source(conn)
    conn.execute(text("UPDATE job_sources SET is_active = false WHERE id = :s"), {"s": source_id})
    feed_returning(monkeypatch, ("ok", 200, [posting(1)], None))
    jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-1"})
    assert (
        conn.execute(text("SELECT count(*) FROM fetch_runs WHERE source_id = :s"), {"s": source_id}).scalar_one() == 0
    )


def test_only_the_three_fixed_hosts_and_plain_tokens_reach_the_network():
    # fetch_postings refuses before any request is made, so this needs no network
    assert asyncio.run(jobs.fetch_feed("greenhouse", "../../internal")) == ("parse_error", None, None, "invalid source")
    assert asyncio.run(jobs.fetch_feed("example", "board")) == ("parse_error", None, None, "invalid source")


def test_the_ingest_handlers_are_registered_with_the_queue():
    assert {"ingest.fetch", "ingest.dedupe", "ingest.discover"} <= set(queue._handlers)


def test_discovery_registers_a_live_board_once_and_reactivates_a_known_one(conn):
    discover.register(conn, [("lever", "fixture-board", "fixture-board", 3)])
    conn.execute(text("UPDATE job_sources SET is_active = false WHERE token = 'fixture-board'"))
    discover.register(conn, [("lever", "fixture-board", "Fixture Co", 3)])
    rows = conn.execute(text("SELECT company_name, is_active FROM job_sources WHERE token = 'fixture-board'")).all()
    assert [tuple(row) for row in rows] == [("Fixture Co", True)]


def test_discovery_keeps_only_boards_that_answer_with_postings(monkeypatch):
    async def answer(client, ats, token):
        if (ats, token) == ("lever", "alive"):
            return "ok", 200, [posting(1)], None
        return "http_error", 404, None, None

    async def no_name(client, token):
        return None

    monkeypatch.setattr(discover, "fetch_postings", answer)
    monkeypatch.setattr(discover, "greenhouse_name", no_name)
    assert asyncio.run(discover.find_live_boards(["alive", "gone"])) == [("lever", "alive", "alive", 1)]
