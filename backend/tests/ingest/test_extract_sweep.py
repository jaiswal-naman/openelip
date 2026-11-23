"""IDX-08: the sweep queues one extraction per open, canonical, listed job whose requirements are not those of its
current text, oldest change first, a bounded number at a time, and only when a real model can be called.

Nothing here calls the gateway: the sweep reads the routing and writes queue rows, so the tests are rolled back in the
`conn` fixture's transaction. Dates are explicit because every row inserted in one transaction would otherwise share
one now(), and "oldest first" could not be told from "any order".
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.ingest import extraction
from app.ingest.requirements import task, validate
from app.platform import config, queue

BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)
VERSION_2_TEXT = "Invented posting for a unit test; Acme Robotics is not a real company.\nYou need Go."


def settings_for(**env):
    """The real settings, so the database is still the test one, under another provider setup."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    return config.load({**os.environ, **blank, "APP_ENV": "test", **env})


@pytest.fixture
def with_a_model(monkeypatch):
    """A keyed Anthropic route: the sweep sees a real model. Nothing is called."""
    monkeypatch.setattr(config, "get", lambda: settings_for(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k"))


@pytest.fixture(autouse=True)
def _log_level(caplog):
    caplog.set_level(logging.INFO, logger="app.event")


def add_source(conn):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
    ).scalar_one()


def add_job(conn, source, n, *, days=0, content_version=2, **over):
    """An open, canonical, listed job at `content_version` whose text last changed `days` after BASE."""
    fields = {
        "source_id": source,
        "external_id": f"job-{n}",
        "title": f"Invented title {n}",
        "company_name": "Acme",
        "description": VERSION_2_TEXT,
        "apply_url": "https://example.test/apply",
        "content_hash": f"hash-{n}",
        "dedupe_key": f"key-{n}",
        "content_version": content_version,
        "materially_changed_at": BASE + timedelta(days=days),
        **over,
    }
    names = ", ".join(fields)
    values = ", ".join(f":{name}" for name in fields)
    return conn.execute(text(f"INSERT INTO jobs ({names}) VALUES ({values}) RETURNING id"), fields).scalar_one()


def queued(conn):
    rows = conn.execute(
        text("SELECT unique_key, queue, payload FROM queue_jobs WHERE kind = 'ingest.extract' ORDER BY id")
    ).all()
    return [(row.unique_key, row.queue, row.payload) for row in rows]


def sweep_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if "ingest.extract_sweep" in r.getMessage()]


# ---- when there is no model ----------------------------------------


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"LLM_PROVIDER": "anthropic"}, "no_key"),  # the default provider with no key: AI is off
        ({"LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "k"}, "no_route"),  # no fallback route yet
        ({"LLM_PROVIDER": "nonsense"}, "no_route"),
        ({"LLM_PROVIDER": "fake", "APP_ENV": "production"}, "fake_in_production"),
        ({"LLM_PROVIDER": "fake"}, "replay_only"),  # the keyless install's default: nothing to answer a live posting
    ],
    ids=["no_key", "no_fallback", "no_such_provider", "fake_in_production", "fake_provider"],
)
def test_with_no_model_to_call_nothing_is_queued_and_the_log_says_why(conn, monkeypatch, caplog, env, reason):
    monkeypatch.setattr(config, "get", lambda: settings_for(**env))
    add_job(conn, add_source(conn), 1)

    extraction.extract_sweep(conn, {})

    assert queued(conn) == []
    (line,) = sweep_lines(caplog)
    assert (line["status"], line["error_code"], line["queued"]) == ("ai_disabled", reason, 0)
    assert set(line) - {"ts", "level", "event", "request_id"} == {"status", "error_code", "queued"}  # no text


# ---- which jobs, in which order ----------------------------------------


def test_it_queues_open_canonical_listed_jobs_without_current_requirements_oldest_change_first(conn, with_a_model):
    source = add_source(conn)
    newest_never = add_job(conn, source, 1, days=3)  # requirements_version NULL
    oldest_stale = add_job(conn, source, 2, days=1, requirements_version=1)  # requirements of an older text
    middle_never = add_job(conn, source, 3, days=2)
    add_job(conn, source, 4, days=0, status="closed", closed_at=BASE)
    add_job(conn, source, 5, days=0, canonical_job_id=newest_never)  # a duplicate: its canonical job is asked about
    add_job(conn, source, 6, days=0, is_listed=False)  # an employer's role
    add_job(conn, source, 7, days=0, requirements_version=2)  # already extracted for this text

    extraction.extract_sweep(conn, {})

    assert queued(conn) == [
        (f"ingest.extract:{job}:2", "ingest", {"job_id": job, "content_version": 2})
        for job in (oldest_stale, middle_never, newest_never)
    ]


def test_a_job_at_a_later_version_is_queued_under_a_key_of_its_own(conn, with_a_model):
    job = add_job(conn, add_source(conn), 1)
    extraction.extract_sweep(conn, {})
    conn.execute(
        text("UPDATE jobs SET description = :d, content_version = 3, materially_changed_at = now() WHERE id = :j"),
        {"d": VERSION_2_TEXT + " Version 3.", "j": job},
    )

    extraction.extract_sweep(conn, {})

    assert [key for key, _, _ in queued(conn)] == [f"ingest.extract:{job}:2", f"ingest.extract:{job}:3"]


def test_a_second_run_queues_no_duplicate(conn, with_a_model, caplog):
    source = add_source(conn)
    for n in (1, 2, 3):
        add_job(conn, source, n, days=n)

    extraction.extract_sweep(conn, {})
    first = queued(conn)
    extraction.extract_sweep(conn, {})

    assert len(first) == 3 and queued(conn) == first
    assert [(line["candidates"], line["queued"]) for line in sweep_lines(caplog)] == [(3, 3), (0, 0)]


def test_one_sweep_queues_at_most_its_limit_and_the_next_sweep_takes_the_rest(conn, with_a_model, monkeypatch):
    assert extraction.SWEEP_LIMIT == 200
    monkeypatch.setattr(extraction, "SWEEP_LIMIT", 2)
    source = add_source(conn)
    jobs = [add_job(conn, source, n, days=n) for n in (1, 2, 3)]

    extraction.extract_sweep(conn, {})
    assert [p["job_id"] for _, _, p in queued(conn)] == jobs[:2]  # the two oldest

    extraction.extract_sweep(conn, {})
    assert [p["job_id"] for _, _, p in queued(conn)] == jobs  # the next sweep, not a repeat of the first


def test_a_job_whose_extraction_already_ended_does_not_hold_up_the_ones_behind_it(conn, with_a_model, monkeypatch):
    monkeypatch.setattr(extraction, "SWEEP_LIMIT", 1)
    source = add_source(conn)
    unusable, waiting = add_job(conn, source, 1, days=1), add_job(conn, source, 2, days=2)
    extraction.extract_sweep(conn, {})
    conn.execute(text("UPDATE queue_jobs SET status = 'dead', finished_at = now()"))  # its answer was unusable

    extraction.extract_sweep(conn, {})  # the dead job's key is still taken: it is not asked again, and not in the way

    assert [p["job_id"] for _, _, p in queued(conn)] == [unusable, waiting]


def test_the_job_the_sweep_queued_is_the_job_the_handler_reads(conn, with_a_model, monkeypatch):
    job = add_job(conn, add_source(conn), 1)
    extraction.extract_sweep(conn, {})
    monkeypatch.setattr(task, "run", lambda description: validate.ValidationResult([], {}))  # no model is called

    queued_job = queue.claim(conn, "test-worker")
    assert queued_job is not None and queued_job.kind == "ingest.extract"
    extraction.extract(conn, queued_job.payload)

    state = conn.execute(text("SELECT requirements_version, content_version FROM jobs WHERE id = :j"), {"j": job}).one()
    assert tuple(state) == (2, 2)
