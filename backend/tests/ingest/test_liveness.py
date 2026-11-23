"""IDX-07, the apply-link re-check: verify_live closes a job whose link is dead, and nothing else.

Jobs are invented fixtures written through store(), the way a fetch writes them. Nothing touches the network: most tests
replace fetch_public with a stand-in that answers a status or raises, and two run the real fetch_public over an
httpx.MockTransport and an invented DNS table. Most run in the `conn` fixture's transaction, which is rolled back; one
transaction has one now(), so a test that wants to see a time move first sets it three days back by hand. The tests that
go through the command line commit, so they empty the job tables before and after.
"""

import functools
import json
import logging

import httpx
import pytest
from sqlalchemy import text

from app import cli
from app.ingest import liveness
from app.ingest.fetch import mark_duplicates, store
from app.platform import db, http

LONG_AGO = "now() - interval '3 days'"
JOB_URL = "https://example.test/jobs/1"


def posting(n, **over):
    base = {
        "external_id": f"live-{n}",
        "title": f"Live engineer {n}",
        "location_raw": "Pune, India",
        "country_hint": None,
        "remote_hint": None,
        "employment_type": None,
        "department": None,
        "description": f"Invented fixture posting number {n}: keep the apply link check honest.",
        "apply_url": f"https://example.test/jobs/{n}",
        "posted_at": None,
    }
    return {**base, **over}


def stored(conn, *postings):
    """Store the postings as one fetch would, and return their job ids in order."""
    source = conn.execute(text("SELECT id FROM job_sources WHERE token = 'live'")).scalar()
    if source is None:
        source = conn.execute(
            text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'live', 'Live Co') RETURNING id")
        ).scalar_one()
    store(conn, source, "Live Co", list(postings))
    return [
        conn.execute(text("SELECT id FROM jobs WHERE external_id = :e"), {"e": p["external_id"]}).scalar_one()
        for p in postings
    ]


def age(conn, job_id, apply_check_ok=None):
    """Make the job look verified and checked three days ago, so a change to now() shows inside one transaction."""
    conn.execute(
        text(
            f"""UPDATE jobs SET last_verified_at = {LONG_AGO}, apply_checked_at = {LONG_AGO}, apply_check_ok = :ok
                 WHERE id = :id"""
        ),
        {"id": job_id, "ok": apply_check_ok},
    )


def job(conn, job_id):
    return conn.execute(
        text(
            """SELECT status::text AS status, apply_check_ok, content_version,
                      closed_at = now() AS closed_now, apply_checked_at = now() AS checked_now,
                      last_verified_at = now() AS verified_now, closed_at IS NULL AS never_closed
                 FROM jobs WHERE id = :id"""
        ),
        {"id": job_id},
    ).one()


def answering(status=None, error=None):
    """A stand-in for fetch_public: answers `status` or raises `error`, and lists the URLs it was asked for."""
    asked = []

    def fetch(url):
        asked.append(url)
        if error is not None:
            raise error
        return http.PublicResponse(status, url, "text/html", b"")

    return fetch, asked


def verify(conn, monkeypatch, job_id, status=None, error=None):
    fetch, asked = answering(status, error)
    monkeypatch.setattr(liveness, "fetch_public", fetch)
    return liveness.verify_live(conn, job_id), asked


# --- the decision table -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_dead_link_closes_the_job(conn, monkeypatch, status):
    (job_id,) = stored(conn, posting(1))
    age(conn, job_id, apply_check_ok=True)

    result, asked = verify(conn, monkeypatch, job_id, status)

    row = job(conn, job_id)
    assert result is False and asked == [JOB_URL]
    assert (row.status, row.apply_check_ok, row.closed_now, row.checked_now) == ("closed", False, True, True)


@pytest.mark.parametrize("status", [200, 204])
def test_an_answer_in_the_2xx_range_marks_the_job_alive_and_verified(conn, monkeypatch, status):
    (job_id,) = stored(conn, posting(1))
    age(conn, job_id)

    result, _ = verify(conn, monkeypatch, job_id, status)

    row = job(conn, job_id)
    assert result is True
    assert (row.status, row.apply_check_ok, row.checked_now, row.verified_now) == ("open", True, True, True)


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 418, 429, 451, 500, 502, 503])
def test_any_other_answer_closes_nothing(conn, monkeypatch, status):
    (job_id,) = stored(conn, posting(1))
    age(conn, job_id, apply_check_ok=True)

    result, _ = verify(conn, monkeypatch, job_id, status)

    row = job(conn, job_id)
    assert result is None
    assert (row.status, row.apply_check_ok, row.checked_now, row.verified_now, row.never_closed) == (
        "open",
        None,
        True,
        False,  # a failed check is not a verification
        True,
    )


@pytest.mark.parametrize(
    "error",
    [
        http.UnsafeUrl("non_public_address"),
        http.FetchFailed("timeout"),
        http.FetchFailed("network"),
        http.FetchFailed("too_large"),
        http.FetchFailed("too_many_redirects"),
    ],
)
def test_a_refusal_or_a_failure_closes_nothing(conn, monkeypatch, error):
    (job_id,) = stored(conn, posting(1))
    age(conn, job_id, apply_check_ok=True)

    result, _ = verify(conn, monkeypatch, job_id, error=error)

    row = job(conn, job_id)
    assert result is None
    assert (row.status, row.apply_check_ok, row.checked_now, row.never_closed) == ("open", None, True, True)


# --- which job, and what if it changed meanwhile ------------------------------------------------------------------


def test_a_job_that_is_already_closed_or_missing_returns_false_without_a_request(conn, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    (job_id,) = stored(conn, posting(1))
    conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id"), {"id": job_id})
    age(conn, job_id)

    closed, asked = verify(conn, monkeypatch, job_id, 200)
    missing, asked_again = verify(conn, monkeypatch, 987654321, 200)

    assert (closed, missing, asked, asked_again) == (False, False, [], [])
    assert job(conn, job_id).checked_now is False  # nothing was written either
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.event"]
    assert [line["outcome"] for line in lines] == ["not_open", "not_open"]


def test_a_duplicate_checks_its_canonical_jobs_link_and_the_result_lands_on_that_job(conn, monkeypatch):
    canonical, duplicate = stored(conn, posting(1), posting(2, title="Live engineer 1"))
    assert mark_duplicates(conn) == 1
    assert (
        conn.execute(text("SELECT canonical_job_id FROM jobs WHERE id = :id"), {"id": duplicate}).scalar() == canonical
    )
    age(conn, canonical)
    age(conn, duplicate)

    result, asked = verify(conn, monkeypatch, duplicate, 404)

    assert result is False and asked == [JOB_URL]  # the canonical job's link, not the duplicate's own
    assert job(conn, canonical).status == "closed"
    assert (job(conn, duplicate).status, job(conn, duplicate).checked_now) == ("open", False)


@pytest.mark.parametrize(("status", "expected"), [(200, False), (404, False), (503, None)])
def test_a_close_that_happens_during_the_request_is_not_touched_by_a_late_answer(conn, monkeypatch, status, expected):
    (job_id,) = stored(conn, posting(1))
    age(conn, job_id)

    def fetch(url):
        # Another worker's fetch closes the job while this request is on the wire.
        conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id"), {"id": job_id})
        return http.PublicResponse(status, url, "text/html", b"")

    monkeypatch.setattr(liveness, "fetch_public", fetch)

    assert liveness.verify_live(conn, job_id) is expected  # a 200 is not reported alive: the job is closed
    row = job(conn, job_id)
    assert (row.status, row.apply_check_ok, row.checked_now) == ("closed", None, False)  # no outcome was written


def test_a_job_closed_by_the_check_stays_closed_for_an_unchanged_posting_and_reopens_for_changed_text(
    conn, monkeypatch
):
    (job_id,) = stored(conn, posting(1))
    source_id = conn.execute(text("SELECT source_id FROM jobs WHERE id = :id"), {"id": job_id}).scalar_one()
    assert verify(conn, monkeypatch, job_id, 404)[0] is False

    store(conn, source_id, "Live Co", [posting(1)])  # the board still lists the same posting
    row = job(conn, job_id)
    assert (row.status, row.apply_check_ok, row.content_version) == ("closed", False, 1)

    store(conn, source_id, "Live Co", [posting(1, description="Invented fixture posting, now rewritten in full.")])
    row = job(conn, job_id)
    assert (row.status, row.apply_check_ok, row.content_version) == ("open", None, 2)


# --- the real fetch_public in the loop ----------------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch):
    """The real fetch_public over a mock transport and an invented DNS table. Returns (answers, asked)."""
    answers, asked = {}, []

    def handler(request):
        # The request goes to the address that passed the check, with the link's own name in the Host header.
        named = f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode('ascii')}"
        asked.append(named)
        return answers[named]

    monkeypatch.setattr(http, "resolve", lambda host: ["93.184.216.34"] if host == "example.test" else [host])
    monkeypatch.setattr(
        liveness, "fetch_public", functools.partial(http.fetch_public, transport=httpx.MockTransport(handler))
    )
    return answers, asked


def test_a_missing_page_closes_the_job_through_the_real_fetch(conn, web):
    answers, asked = web
    (job_id,) = stored(conn, posting(1))
    answers[JOB_URL] = httpx.Response(404, headers={"content-type": "text/html"}, content=b"not found")

    assert liveness.verify_live(conn, job_id) is False
    assert asked == [JOB_URL] and job(conn, job_id).status == "closed"


def test_an_apply_link_into_the_private_network_is_never_requested_and_closes_nothing(conn, web):
    _, asked = web
    (job_id,) = stored(conn, posting(1, apply_url="http://169.254.169.254/latest/meta-data/"))

    assert liveness.verify_live(conn, job_id) is None

    assert asked == []  # refused before any request
    row = job(conn, job_id)
    assert (row.status, row.apply_check_ok, row.checked_now) == ("open", None, True)


# --- logs ---------------------------------------------------------------------------------------------------------


def test_the_log_line_has_the_job_id_outcome_and_duration_and_never_the_link(conn, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    (job_id,) = stored(conn, posting(1, apply_url="https://example.test/jobs/1?canary=secret-token"))

    verify(conn, monkeypatch, job_id, 404)

    (line,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.event"]
    assert line["event"] == "ingest.verify_live" and line["job_id"] == job_id and line["outcome"] == "closed"
    assert isinstance(line["duration_ms"], int)
    assert "secret-token" not in caplog.text and "example.test" not in caplog.text


@pytest.mark.parametrize(("status", "outcome"), [(200, "alive"), (404, "closed"), (503, "unknown")])
def test_the_outcome_names_what_happened(conn, monkeypatch, caplog, status, outcome):
    caplog.set_level(logging.INFO, logger="app.event")
    (job_id,) = stored(conn, posting(1))

    verify(conn, monkeypatch, job_id, status)

    (line,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.event"]
    assert line["outcome"] == outcome


# --- the command line ---------------------------------------------------------------------------------------------


@pytest.fixture
def committed_index(engine):
    """The command opens its own worker connection and commits, so this empties the tables before and after."""
    empty = text("TRUNCATE fetch_runs, jobs, job_sources RESTART IDENTITY CASCADE")
    with engine.begin() as c:
        c.execute(empty)
    yield
    with engine.begin() as c:
        c.execute(empty)


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err.strip()


def test_the_command_is_parsed_and_has_a_handler():
    args = cli.build_parser().parse_args(["jobs", "verify", "7"])
    assert (args.group, args.command, args.job_id) == ("jobs", "verify", 7)
    assert ("jobs", "verify") in cli.COMMANDS
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["jobs", "verify", "not-a-number"])


@pytest.mark.parametrize(
    ("status", "word", "code", "stored_status"),
    [(200, "alive", 0, "open"), (404, "closed", 0, "closed"), (503, "unknown", 3, "open")],
)
def test_the_command_prints_alive_closed_or_unknown_and_exits_0_0_or_3(
    engine, committed_index, monkeypatch, capsys, status, word, code, stored_status
):
    with engine.begin() as c:
        (job_id,) = stored(c, posting(1))
    fetch, asked = answering(status)
    monkeypatch.setattr(liveness, "fetch_public", fetch)

    assert run_cli(capsys, "jobs", "verify", str(job_id)) == (code, word, "")

    assert asked == [JOB_URL]
    with engine.connect() as c:  # the command committed its own transaction
        assert c.execute(text("SELECT status::text FROM jobs WHERE id = :id"), {"id": job_id}).scalar() == stored_status


def test_a_job_the_index_does_not_have_exits_2_and_asks_nothing(committed_index, monkeypatch, capsys):
    fetch, asked = answering(200)
    monkeypatch.setattr(liveness, "fetch_public", fetch)

    code, out, err = run_cli(capsys, "jobs", "verify", "987654321")

    assert (code, out, asked) == (2, "", [])
    assert "987654321" in err


def test_an_already_closed_job_is_reported_closed_without_a_request(engine, committed_index, monkeypatch, capsys):
    with engine.begin() as c:
        (job_id,) = stored(c, posting(1))
        c.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id"), {"id": job_id})
    fetch, asked = answering(200)
    monkeypatch.setattr(liveness, "fetch_public", fetch)

    assert run_cli(capsys, "jobs", "verify", str(job_id)) == (0, "closed", "")
    assert asked == []


def test_no_row_lock_is_held_while_the_request_is_on_the_wire(engine, committed_index, monkeypatch):
    with engine.begin() as c:
        (job_id,) = stored(c, posting(1))

    def fetch(url):
        # Another session takes the lock the check would hold if it kept one: NOWAIT fails at once when it is held.
        with engine.begin() as other:
            other.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE NOWAIT"), {"id": job_id})
        return http.PublicResponse(200, url, "text/html", b"")

    monkeypatch.setattr(liveness, "fetch_public", fetch)

    with db.transaction(role="worker") as conn:
        assert liveness.verify_live(conn, job_id) is True
