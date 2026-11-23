"""Ingest writes the typed job shape: work arrangements, employment type, versions, and what a new version clears.

Feeds are invented fixtures shaped like the adapters' output; nothing here touches the network. Every test runs in the
`conn` fixture's transaction, which is rolled back. One transaction has one now(), so a test that wants to see a time
move first sets it to an earlier value by hand.
"""

import pytest
from sqlalchemy import text

from app.ingest import jobs
from app.ingest.fetch import store
from app.ingest.normalize import employment_type_from, work_arrangements_from

LONG_AGO = "now() - interval '3 days'"


def posting(n, description=None, **over):
    base = {
        "external_id": f"typed-{n}",
        "title": f"Typed engineer {n}",
        "location_raw": "Pune, India",
        "country_hint": None,
        "remote_hint": None,
        "employment_type": None,
        "department": None,
        "description": description or f"Invented fixture posting number {n}: build and run the typed services.",
        "apply_url": f"https://example.test/jobs/{n}",
        "posted_at": None,
    }
    return {**base, **over}


def new_source(conn):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'typed', 'Typed Co') RETURNING id")
    ).scalar_one()


def job(conn, external_id):
    return conn.execute(
        text(
            """SELECT id, country, city, work_arrangements::text[] AS work_arrangements, employment_type::text AS kind,
                      employment_type_raw, content_version, status::text AS status, closed_at,
                      embedding IS NULL AS no_embedding, embedding_model, apply_checked_at, apply_check_ok,
                      materially_changed_at, first_seen_at, last_verified_at
                 FROM jobs WHERE external_id = :e"""
        ),
        {"e": external_id},
    ).one()


@pytest.mark.parametrize(("is_remote", "expected"), [(True, ["remote"]), (False, ["onsite", "hybrid"]), (None, [])])
def test_work_arrangements_follow_the_data_model_rule(is_remote, expected):
    assert work_arrangements_from(is_remote) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Full-time", "full_time"),
        ("FullTime", "full_time"),
        ("Part-time", "part_time"),
        ("Contract", "contract"),
        ("Internship", "internship"),
        ("Temporary", "temporary"),
        ("Seasonal", "unknown"),  # not plainly one of them
        ("Independent Contractor", "unknown"),  # does not start with a known word, so it is not guessed
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_employment_type_is_mapped_only_where_the_wording_plainly_matches(raw, expected):
    assert employment_type_from(raw) == expected


def test_a_new_posting_is_stored_with_the_typed_columns(conn):
    source_id = new_source(conn)
    store(conn, source_id, "Typed Co", [posting(1, employment_type="Full-time", remote_hint=True)])
    row = job(conn, "typed-1")
    assert (row.work_arrangements, row.kind, row.employment_type_raw) == (["remote"], "full_time", "Full-time")
    assert (row.country, row.city, row.content_version) == ("IN", "Pune", 1)
    assert row.materially_changed_at == row.first_seen_at  # set at first sight

    store(conn, source_id, "Typed Co", [posting(2, employment_type="Seasonal")])
    row = job(conn, "typed-2")
    assert (row.work_arrangements, row.kind, row.employment_type_raw) == ([], "unknown", "Seasonal")


def test_a_changed_posting_is_a_new_version_and_loses_what_belonged_to_the_old_text(conn):
    source_id = new_source(conn)
    store(conn, source_id, "Typed Co", [posting(1)])
    conn.execute(
        text(
            f"""UPDATE jobs SET embedding = array_fill(0.1::real, ARRAY[384])::vector, embedding_model = 'test-model',
                       apply_checked_at = now(), apply_check_ok = true, materially_changed_at = {LONG_AGO}
                 WHERE external_id = 'typed-1'"""
        )
    )
    before = job(conn, "typed-1")
    assert (before.no_embedding, before.apply_check_ok) == (False, True)

    result = store(conn, source_id, "Typed Co", [posting(1, description="Invented fixture posting, now rewritten.")])

    row = job(conn, "typed-1")
    assert result["changed"] == 1 and row.content_version == 2
    assert (row.no_embedding, row.embedding_model) == (True, None)
    assert (row.apply_checked_at, row.apply_check_ok) == (None, None)
    assert row.materially_changed_at > before.materially_changed_at  # moved from three days ago to this transaction
    assert row.materially_changed_at == row.last_verified_at


def test_an_unchanged_posting_is_verified_again_and_its_place_is_read_afresh(conn):
    source_id = new_source(conn)
    feed = [posting(1, remote_hint=False)]
    store(conn, source_id, "Typed Co", feed)
    first = job(conn, "typed-1")
    # An old parser got the place wrong; the text has not changed since.
    conn.execute(
        text(
            f"""UPDATE jobs SET country = 'US', city = 'Austin', work_arrangements = '{{remote}}',
                       dedupe_key = 'stale', last_verified_at = {LONG_AGO} WHERE id = :id"""
        ),
        {"id": first.id},
    )

    result = store(conn, source_id, "Typed Co", feed)

    row = job(conn, "typed-1")
    assert (result["new"], result["changed"]) == (0, 0) and row.content_version == 1
    assert (row.country, row.city, row.work_arrangements) == ("IN", "Pune", ["onsite", "hybrid"])
    assert row.last_verified_at == row.first_seen_at  # verified in this transaction, not three days ago
    assert conn.execute(text("SELECT dedupe_key FROM jobs WHERE id = :id"), {"id": first.id}).scalar_one() != "stale"


def mark_closed_by_the_apply_link_check(conn, job_id):
    conn.execute(
        text(
            """UPDATE jobs SET status = 'closed', closed_at = now(), apply_checked_at = now(), apply_check_ok = false
                WHERE id = :id"""
        ),
        {"id": job_id},
    )


def test_a_job_the_apply_link_check_closed_stays_closed_until_its_text_changes(conn):
    source_id = new_source(conn)
    feed = [posting(1)]
    store(conn, source_id, "Typed Co", feed)
    mark_closed_by_the_apply_link_check(conn, job(conn, "typed-1").id)

    store(conn, source_id, "Typed Co", feed)  # the feed still lists it, with the same text
    row = job(conn, "typed-1")
    assert (row.status, row.apply_check_ok, row.content_version) == ("closed", False, 1)

    store(conn, source_id, "Typed Co", [posting(1, description="Invented fixture posting, now rewritten.")])
    row = job(conn, "typed-1")
    assert (row.status, row.closed_at, row.apply_check_ok, row.content_version) == ("open", None, None, 2)


def test_a_job_that_only_left_its_feed_reopens_when_it_comes_back_unchanged(conn):
    source_id = new_source(conn)
    feed = [posting(1), posting(2)]
    store(conn, source_id, "Typed Co", feed)
    store(conn, source_id, "Typed Co", feed[:1])  # posting 2 left its feed and was closed
    assert job(conn, "typed-2").status == "closed"

    store(conn, source_id, "Typed Co", feed)  # and came back
    assert job(conn, "typed-2").status == "open"


def failures(conn, source_id):
    return conn.execute(
        text("SELECT consecutive_failures FROM job_sources WHERE id = :s"), {"s": source_id}
    ).scalar_one()


def test_a_failed_or_suspicious_fetch_adds_a_failure_and_an_ok_fetch_resets_the_count(conn, monkeypatch):
    source_id = new_source(conn)
    busy = [posting(n) for n in range(6)]  # more than five open jobs, so an empty feed is suspicious
    outcomes = {
        "ok": ("ok", 200, busy, None),
        "http_error": ("http_error", 503, None, None),
        "network_error": ("network_error", None, None, "ConnectError"),
        "suspicious_empty": ("ok", 200, [], None),
    }

    def run(name):
        async def fake(ats, token):
            return outcomes[name]

        monkeypatch.setattr(jobs, "fetch_feed", fake)
        jobs.fetch_source(conn, {"source_id": source_id, "slot": "slot-1"})

    run("ok")
    assert failures(conn, source_id) == 0
    for expected, name in enumerate(["http_error", "network_error", "suspicious_empty"], start=1):
        run(name)
        assert failures(conn, source_id) == expected
        last_status = conn.execute(
            text("SELECT last_status::text FROM job_sources WHERE id = :s"), {"s": source_id}
        ).scalar_one()
        assert last_status == name
        assert conn.execute(text("SELECT count(*) FROM jobs WHERE status = 'open'")).scalar_one() == 6  # nothing closed
    run("ok")
    assert failures(conn, source_id) == 0
