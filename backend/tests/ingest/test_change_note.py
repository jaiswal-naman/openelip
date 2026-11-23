"""MAT-07, the ingest half (plan 04-03, task 3): when a stored posting gets a new content version, ingest says in
a short sentence which of the words that make up its content changed, so a match shown again can say what is different.

Only title, location and description feed the content hash (ingest stores no pay or seniority), so nothing else is ever
named. The sentences are made by code, in that order. Postings are invented. Every test runs in the `conn` fixture's
transaction, which is rolled back.
"""

from sqlalchemy import text

from app.ingest.fetch import store

COMPANY = "Fixture Co"


def posting(*, title="Fixture engineer", location="Pune, India", description=None):
    return {
        "external_id": "fixture-1",
        "title": title,
        "location_raw": location,
        "country_hint": None,
        "remote_hint": None,
        "employment_type": None,
        "department": None,
        "description": description or "Invented fixture posting: build and run the fixture services every day.",
        "apply_url": "https://example.test/jobs/1",
        "posted_at": None,
    }


def stored(conn):
    row = conn.execute(text("SELECT content_version, change_note FROM jobs WHERE external_id = 'fixture-1'")).one()
    return row.content_version, row.change_note


def seen(conn, source_id, **changes):
    return store(conn, source_id, COMPANY, [posting(**changes)])


def source(conn):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('greenhouse', 'fixture', :c) RETURNING id"),
        {"c": COMPANY},
    ).scalar_one()


def test_a_posting_seen_for_the_first_time_has_no_note(conn):
    source_id = source(conn)

    assert seen(conn, source_id)["new"] == 1

    assert stored(conn) == (1, None)


def test_a_changed_title_is_named(conn):
    source_id = source(conn)
    seen(conn, source_id)

    assert seen(conn, source_id, title="Senior fixture engineer")["changed"] == 1

    assert stored(conn) == (2, "Title changed.")


def test_a_changed_location_and_description_are_named_in_that_order(conn):
    source_id = source(conn)
    seen(conn, source_id)

    seen(conn, source_id, location="Mumbai, India", description="Invented fixture posting, now with other words in it.")

    assert stored(conn) == (2, "Location changed. The posting text changed.")


def test_all_three_are_named_when_all_three_changed(conn):
    source_id = source(conn)
    seen(conn, source_id)

    seen(
        conn,
        source_id,
        title="Other",
        location="Berlin, Germany",
        description="Invented, wholly rewritten posting text.",
    )

    assert stored(conn) == (2, "Title changed. Location changed. The posting text changed.")


def test_each_new_version_replaces_the_note_with_what_changed_in_it(conn):
    source_id = source(conn)
    seen(conn, source_id)
    seen(conn, source_id, title="Senior fixture engineer")

    seen(conn, source_id, title="Senior fixture engineer", location="Mumbai, India")

    assert stored(conn) == (3, "Location changed.")


def test_the_same_words_fetched_again_bump_nothing_and_keep_the_note(conn):
    source_id = source(conn)
    seen(conn, source_id)
    assert seen(conn, source_id)["changed"] == 0  # unchanged: no version, no note
    assert stored(conn) == (1, None)

    seen(conn, source_id, title="Senior fixture engineer")
    again = seen(conn, source_id, title="Senior fixture engineer")

    assert again["changed"] == 0
    assert stored(conn) == (2, "Title changed.")


def test_a_new_version_with_no_visible_difference_has_no_note_rather_than_an_empty_one(conn):
    source_id = source(conn)
    seen(conn, source_id)
    conn.execute(text("UPDATE jobs SET content_hash = 'made-by-an-older-hash'"))  # the stored hash no longer matches

    assert seen(conn, source_id)["changed"] == 1  # a new version, though no field differs

    assert stored(conn) == (2, None)
