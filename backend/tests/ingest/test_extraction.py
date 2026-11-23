"""IDX-08: each open, canonical, listed job has its requirements extracted once per content version.

The model is Phase 1's fake provider answering from recordings in fixtures/extraction/, which are invented (see the
README there). The gateway's two ledger writes are replaced by a list, as plan 02-03's tests do, so these tests need no
model, no key and no ledger; routing, the replay and the schema check all run for real. Almost every test runs in the
`conn` fixture's transaction, which is rolled back; the last one uses the worker's own database login.
"""

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.ingest import api, extraction, jobs, liveness
from app.ingest.requirements import rules, segment, task, validate
from app.platform import config, db, queue, scheduler
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, fake, gateway, registry

FIXTURES = Path(__file__).parent / "fixtures" / "extraction"
POSTING_V2 = (FIXTURES / "invented_posting_v2.txt").read_text(encoding="utf-8").rstrip("\n")
POSTING_V3 = (FIXTURES / "invented_posting_v3.txt").read_text(encoding="utf-8").rstrip("\n")
A_VECTOR = "[" + ",".join(["0.5"] * 384) + "]"
NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)


def settings_for(**env: str) -> config.Settings:
    """The real settings, so the database is still the test one, under another provider setup."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    return config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake", **env})


@pytest.fixture
def gateway_calls(monkeypatch):
    """The provider requests the gateway makes, as (task, prompt_version, principal, attempt, outcome). The fake
    provider answers from the recording a test names with answers_from()."""
    calls = []
    monkeypatch.setattr(config, "get", lambda: settings_for())
    monkeypatch.setattr(registry, "model_id", lambda route: UUID(int=1))
    monkeypatch.setattr(
        gateway,
        "_record",
        lambda route, model_id, prompt_version, principal, attempt, outcome, reply, started: calls.append(
            (route.task, prompt_version, principal, attempt, outcome)
        ),
    )
    yield calls
    fake.replay_from(None)


def answers_from(name):
    fake.replay_from(FIXTURES / name)  # starts the list over


def add_source(conn):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
    ).scalar_one()


def add_job(conn, source, n, *, description=POSTING_V2, content_version=2, **over):
    fields = {
        "source_id": source,
        "external_id": f"job-{n}",
        "title": f"Invented title {n}",
        "company_name": "Acme",
        "description": description,
        "apply_url": "https://example.test/apply",
        "content_hash": f"hash-{n}",
        "dedupe_key": f"key-{n}",
        "content_version": content_version,
        **over,
    }
    names = ", ".join(fields)
    values = ", ".join(f":{name}" for name in fields)
    return conn.execute(text(f"INSERT INTO jobs ({names}) VALUES ({values}) RETURNING id"), fields).scalar_one()


def job_state(conn, job_id):
    return conn.execute(
        text(
            """SELECT content_version, requirements_version, embedding IS NULL AS no_vector, embedding_model
                 FROM jobs WHERE id = :id"""
        ),
        {"id": job_id},
    ).one()


def requirement_rows(conn, job_id):
    return conn.execute(
        text(
            """SELECT content_version, ordinal, importance::text AS importance, kind::text AS kind, text,
                      lower(span) AS start, upper(span) AS "end", min_years, prompt_version, llm_call_id
                 FROM job_requirements WHERE job_id = :id ORDER BY content_version, ordinal"""
        ),
        {"id": job_id},
    ).all()


def payload(job_id, version):
    return {"job_id": job_id, "content_version": version}


def events(caplog, name):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.event" and name in r.getMessage()]


@pytest.fixture(autouse=True)
def _log_level(caplog):
    caplog.set_level(logging.INFO, logger="app.event")


# ---- one extraction ----------------------------------------


def test_an_open_job_is_extracted_once_with_rows_that_quote_its_text(conn, gateway_calls, caplog):
    job = add_job(conn, add_source(conn), 1, embedding=A_VECTOR, embedding_model="older-model")
    answers_from("invented_answer_v2.json")

    extraction.extract(conn, payload(job, 2))

    rows = requirement_rows(conn, job)
    assert [(r.content_version, r.ordinal, r.importance, r.kind, r.text, r.min_years) for r in rows] == [
        (2, 1, "required", "experience", "5+ years of Go", 5),
        (2, 2, "required", "education", "Bachelor's degree in CS", None),
    ]
    for row in rows:
        assert POSTING_V2[row.start : row.end] == row.text  # the span is the range code computed, and it resolves
        assert row.prompt_version == task.PROMPT_VERSION
        assert row.llm_call_id is None  # the gateway returns no ledger id, and one is never guessed
    state = job_state(conn, job)
    assert (state.requirements_version, state.content_version) == (2, 2)
    assert (state.no_vector, state.embedding_model) == (True, None)  # the embed sweep re-embeds it from the rows
    assert gateway_calls == [(task.TASK, task.PROMPT_VERSION, None, 1, "ok")]  # one request, charged to no person
    (line,) = events(caplog, "ingest.extract")
    assert (line["job_id"], line["outcome"], line["rows"], line["rejected"]) == (job, "extracted", 2, 0)


def test_a_second_run_for_the_same_version_calls_no_model_and_writes_nothing(conn, gateway_calls, caplog):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_v2.json")
    extraction.extract(conn, payload(job, 2))
    before = requirement_rows(conn, job)

    extraction.extract(conn, payload(job, 2))  # a second call would find no recorded answer left and fail

    assert len(gateway_calls) == 1
    assert requirement_rows(conn, job) == before
    assert events(caplog, "ingest.extract")[-1]["outcome"] == "skipped"


def test_a_changed_posting_is_extracted_again_and_the_earlier_rows_remain(conn, gateway_calls):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_v2.json")
    extraction.extract(conn, payload(job, 2))
    conn.execute(
        text(
            "UPDATE jobs SET description = :d, content_version = 3, embedding = NULL, embedding_model = NULL "
            "WHERE id = :j"
        ),
        {"d": POSTING_V3, "j": job},
    )  # what a fetch does when the posting's text changes

    answers_from("invented_answer_v3.json")
    extraction.extract(conn, payload(job, 3))

    rows = requirement_rows(conn, job)
    assert [(r.content_version, r.ordinal, r.text) for r in rows] == [
        (2, 1, "5+ years of Go"),
        (2, 2, "Bachelor's degree in CS"),  # version 2's rows are still there
        (3, 1, "7+ years of Go"),
        (3, 2, "Experience with Kubernetes"),
    ]
    assert all(POSTING_V3[r.start : r.end] == r.text for r in rows if r.content_version == 3)  # each version's own text
    assert rows[3].importance == "preferred"
    assert job_state(conn, job).requirements_version == 3
    assert len(gateway_calls) == 2  # one request for each version


def test_a_valid_answer_with_no_requirements_marks_the_version_done_and_adds_no_rows(conn, gateway_calls):
    job = add_job(conn, add_source(conn), 1, embedding=A_VECTOR, embedding_model="older-model")
    answers_from("invented_answer_empty.json")

    extraction.extract(conn, payload(job, 2))

    assert requirement_rows(conn, job) == []
    assert job_state(conn, job).requirements_version == 2  # a posting with no stated requirements is not asked again
    extraction.extract(conn, payload(job, 2))
    assert len(gateway_calls) == 1


# ---- the posting moved, or was never to be extracted ----------------------------------------


def test_a_job_whose_text_changed_during_the_call_gets_nothing_and_the_handler_ends_without_error(
    conn, gateway_calls, monkeypatch, caplog
):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_v2.json")
    real_run = task.run

    def a_fetch_lands_while_the_model_works(description):
        result = real_run(description)
        conn.execute(
            text("UPDATE jobs SET description = :d, content_version = 3 WHERE id = :j"),
            {"d": POSTING_V3, "j": job},
        )
        return result

    monkeypatch.setattr(task, "run", a_fetch_lands_while_the_model_works)

    extraction.extract(conn, payload(job, 2))  # no exception: the answer was for text that is gone

    assert requirement_rows(conn, job) == []
    state = job_state(conn, job)
    assert (state.content_version, state.requirements_version) == (3, None)  # still waiting for version 3
    assert events(caplog, "ingest.extract")[-1]["outcome"] == "stale"


def test_a_version_that_another_worker_stored_first_is_left_as_that_worker_wrote_it(
    conn, gateway_calls, monkeypatch, caplog
):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_v2.json")
    real_run = task.run
    quote = "5+ years of Go"

    def a_second_worker_finishes_first(description):
        result = real_run(description)
        conn.execute(
            text(
                """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                 prompt_version)
                   VALUES (:j, 2, 1, 'required', 'skill', :q, int4range(:s, :e), 'other-worker')"""
            ),
            {"j": job, "q": quote, "s": POSTING_V2.index(quote), "e": POSTING_V2.index(quote) + len(quote)},
        )
        conn.execute(text("UPDATE jobs SET requirements_version = 2 WHERE id = :j"), {"j": job})
        return result

    monkeypatch.setattr(task, "run", a_second_worker_finishes_first)

    extraction.extract(conn, payload(job, 2))  # no duplicate-row error: this answer is simply not stored

    assert [(r.ordinal, r.prompt_version) for r in requirement_rows(conn, job)] == [(1, "other-worker")]
    assert events(caplog, "ingest.extract")[-1]["outcome"] == "stale"


@pytest.mark.parametrize(
    "over",
    [{"status": "closed", "closed_at": NOW}, {"is_listed": False}, {"requirements_version": 2}],
    ids=["closed", "unlisted", "already_extracted"],
)
def test_a_job_that_is_not_to_be_extracted_is_skipped_without_a_call(conn, gateway_calls, caplog, over):
    job = add_job(conn, add_source(conn), 1, **over)
    answers_from("invented_answer_v2.json")

    extraction.extract(conn, payload(job, 2))

    assert gateway_calls == [] and requirement_rows(conn, job) == []
    assert events(caplog, "ingest.extract")[-1]["outcome"] == "skipped"


def test_a_duplicate_a_missing_job_and_a_payload_for_another_version_are_skipped_without_a_call(conn, gateway_calls):
    source = add_source(conn)
    canonical = add_job(conn, source, 1)
    duplicate = add_job(conn, source, 2, canonical_job_id=canonical)
    answers_from("invented_answer_v2.json")

    extraction.extract(conn, payload(duplicate, 2))  # its canonical job carries the requirements
    extraction.extract(conn, payload(999_999, 2))  # deleted after the job was queued
    extraction.extract(conn, payload(canonical, 1))  # queued for a version the posting has moved past

    assert gateway_calls == []
    assert requirement_rows(conn, canonical) == requirement_rows(conn, duplicate) == []
    assert job_state(conn, canonical).requirements_version is None


# ---- failures ----------------------------------------


def raising(failure):
    def run(description):
        raise failure

    return run


@pytest.mark.parametrize(
    ("kind", "failure"),
    [
        ("unavailable", AIDisabled("no_key")),
        ("provider_error", ProviderUnavailable()),
        ("timeout", ProviderUnavailable(timed_out=True)),
    ],
)
def test_no_route_a_provider_error_and_a_timeout_fail_the_job_so_that_it_is_retried(
    conn, gateway_calls, monkeypatch, caplog, kind, failure
):
    job = add_job(conn, add_source(conn), 1, embedding=A_VECTOR, embedding_model="older-model")
    monkeypatch.setattr(task, "run", raising(failure))

    with pytest.raises(type(failure)) as raised:
        extraction.extract(conn, payload(job, 2))

    assert not isinstance(raised.value, queue.NonRetryable)  # the queue backs off and tries again
    assert requirement_rows(conn, job) == []
    state = job_state(conn, job)
    assert (state.requirements_version, state.no_vector) == (None, False)  # nothing changed
    (line,) = events(caplog, "ingest.extract")
    assert set(line) - {"ts", "level", "event"} == {"job_id", "error_code", "duration_ms"}  # nothing else
    assert (line["job_id"], line["error_code"], line["level"]) == (job, kind, "warning")


@pytest.mark.parametrize(
    ("kind", "failure"),
    [("invalid_output", SchemaInvalid()), ("refused", SchemaInvalid(refused=True))],
)
def test_an_invalid_or_refused_answer_ends_the_job_for_good_with_no_rows(
    conn, gateway_calls, monkeypatch, caplog, kind, failure
):
    job = add_job(conn, add_source(conn), 1)
    monkeypatch.setattr(task, "run", raising(failure))

    with pytest.raises(queue.NonRetryable) as raised:
        extraction.extract(conn, payload(job, 2))

    assert str(raised.value) == kind  # what the queue may keep: a word, never an answer
    assert requirement_rows(conn, job) == [] and job_state(conn, job).requirements_version is None
    (line,) = events(caplog, "ingest.extract")
    assert (line["job_id"], line["error_code"]) == (job, kind)


def test_an_answer_that_is_not_json_twice_comes_through_the_gateway_as_a_permanent_failure(conn, gateway_calls):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_unusable.json")

    with pytest.raises(queue.NonRetryable):
        extraction.extract(conn, payload(job, 2))

    assert [outcome for *_, outcome in gateway_calls] == [
        "schema_invalid",
        "schema_invalid",
    ]  # one fresh call, then stop
    assert requirement_rows(conn, job) == []


@contextmanager
def the_same_connection(conn):
    yield conn


def run_in_the_queue(conn, job, version):
    queue.enqueue(
        conn, "ingest.extract", payload(job, version), unique_key=f"ingest.extract:{job}:{version}", queue="ingest"
    )
    queue.run_once(lambda: the_same_connection(conn), "test-worker")
    return conn.execute(text("SELECT status::text, last_error_code FROM queue_jobs")).one()


def test_the_queue_retries_a_provider_failure_and_buries_an_unusable_answer_at_once(conn, gateway_calls, monkeypatch):
    first = add_job(conn, add_source(conn), 1)
    monkeypatch.setattr(task, "run", raising(ProviderUnavailable(timed_out=True)))
    assert tuple(run_in_the_queue(conn, first, 2)) == ("queued", "ProviderUnavailable")  # waits, then runs again
    conn.execute(text("DELETE FROM queue_jobs"))

    monkeypatch.setattr(task, "run", raising(SchemaInvalid()))
    assert tuple(run_in_the_queue(conn, first, 2)) == ("dead", "ExtractionRejected")  # never run again


def test_the_queue_marks_a_good_extraction_succeeded(conn, gateway_calls):
    job = add_job(conn, add_source(conn), 1)
    answers_from("invented_answer_v2.json")

    assert tuple(run_in_the_queue(conn, job, 2)) == ("succeeded", None)
    assert len(requirement_rows(conn, job)) == 2


def test_posting_text_never_reaches_a_log_line_whatever_the_outcome(conn, gateway_calls, monkeypatch, caplog):
    canary = "CANARY-posting-text-4f19"
    source = add_source(conn)
    job = add_job(conn, source, 1, description=f"Invented posting. {canary}\nYou need Go.")
    moving = add_job(conn, source, 2, description=f"Invented posting. {canary}\nYou need Rust.")
    answers_from("invented_answer_unusable.json")
    with pytest.raises(queue.NonRetryable):
        extraction.extract(conn, payload(job, 2))  # failed, through the real gateway
    answers_from("invented_answer_empty.json")
    extraction.extract(conn, payload(job, 2))  # extracted
    extraction.extract(conn, payload(job, 2))  # skipped
    real_run = task.run

    def the_posting_changes_meanwhile(description):
        result = real_run(description)
        conn.execute(
            text("UPDATE jobs SET description = :d, content_version = 3 WHERE id = :j"),
            {"d": description + " Changed.", "j": moving},
        )
        return result

    monkeypatch.setattr(task, "run", the_posting_changes_meanwhile)
    answers_from("invented_answer_empty.json")
    extraction.extract(conn, payload(moving, 2))  # stale

    lines = events(caplog, "ingest.extract")
    assert [line.get("error_code") or line["outcome"] for line in lines] == [
        "invalid_output",
        "extracted",
        "skipped",
        "stale",
    ]  # every outcome was logged, so the check below means something
    assert canary not in caplog.text


# ---- wiring ----------------------------------------


def test_the_handler_and_the_fifteen_minute_sweep_are_registered_beside_the_fetch_and_embed_jobs(conn):
    assert queue._handlers["ingest.extract"] is extraction.extract
    assert queue._handlers["ingest.extract_sweep"] is extraction.extract_sweep
    assert jobs.EXTRACT_EVERY_MINUTES == 15 and scheduler._every["ingest.extract_sweep"] == 15 * 60
    scheduler.tick(conn, NOW)
    scheduler.tick(conn, NOW)  # another worker, the same moment
    keys = conn.execute(text("SELECT unique_key FROM queue_jobs WHERE kind = 'ingest.extract_sweep'")).scalars().all()
    assert len(keys) == 1


def test_the_interface_exports_what_other_modules_may_use():
    assert api.verify_live is liveness.verify_live
    assert api.segments is segment.segments and api.to_rows is validate.to_rows
    assert api.rules_extract is rules.extract
    assert {"verify_live", "segments", "to_rows", "rules_extract", "parse_location", "search", "get_job"} <= set(
        api.__all__
    )


# ---- the worker's own login ----------------------------------------


@pytest.fixture
def committed_index(engine):
    """The worker's own login commits, so this test cleans up the way the API tests do: empty before and after."""
    empty = text("TRUNCATE queue_jobs, job_requirements, jobs, job_sources RESTART IDENTITY CASCADE")
    with engine.begin() as c:
        c.execute(empty)
    yield
    with engine.begin() as c:
        c.execute(empty)


def test_the_worker_role_holds_every_privilege_the_extraction_needs(engine, committed_index, gateway_calls):
    with engine.begin() as c:
        job = add_job(c, add_source(c), 1, embedding=A_VECTOR, embedding_model="older-model")
    answers_from("invented_answer_v2.json")

    with db.transaction(role="worker") as conn:
        extraction.extract(conn, payload(job, 2))

    with engine.connect() as c:
        assert [r.text for r in requirement_rows(c, job)] == ["5+ years of Go", "Bachelor's degree in CS"]
        state = job_state(c, job)
        assert (state.requirements_version, state.no_vector) == (2, True)
