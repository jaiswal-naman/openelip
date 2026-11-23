"""The refusals of the wind tunnel (T-08-19, T-08-22; INDEX decisions 5 and 37)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.evals.windtunnel import guard
from app.platform import config, db, queue, scheduler
from tests.evals.windtunnel.conftest import empty
from tests.match.factories import make_user


@pytest.fixture(autouse=True)
def tables(engine):
    empty(engine)
    yield
    empty(engine)


def settings_with(monkeypatch, **urls):
    real = config.get()
    values = {
        "DATABASE_URL_MIGRATE": real.database_url_migrate,
        "DATABASE_URL_API": real.database_url_api,
        "DATABASE_URL_WORKER": real.database_url_worker,
        "DATABASE_URL_MATCH": real.database_url_match,
        **urls,
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **values})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make them again


def test_it_passes_on_the_test_database():
    assert guard.require_test_database().endswith("_test")


@pytest.mark.parametrize("login", ["MIGRATE", "API", "WORKER", "MATCH"])
def test_it_refuses_a_login_that_names_another_database(monkeypatch, login):
    url = getattr(config.get(), f"database_url_{login.lower()}")
    from sqlalchemy.engine import make_url

    other = make_url(url).set(database="app").render_as_string(hide_password=False)
    settings_with(monkeypatch, **{f"DATABASE_URL_{login}": other})
    with pytest.raises(guard.NotATestDatabase, match="does not end in _test"):
        guard.require_test_database()


def test_it_refuses_logins_that_name_different_test_databases(monkeypatch):
    from sqlalchemy.engine import make_url

    other = make_url(config.get().database_url_worker).set(database="other_test").render_as_string(hide_password=False)
    settings_with(monkeypatch, DATABASE_URL_WORKER=other)
    with pytest.raises(guard.NotATestDatabase, match="different databases"):
        guard.require_test_database()


@pytest.mark.parametrize("login", ["MIGRATE", "API", "WORKER", "MATCH"])
def test_it_refuses_a_url_whose_path_is_a_test_database_but_whose_dbname_is_not(monkeypatch, login):
    """A `dbname` query parameter overrides the URL's path: the server's own answer decides, before any row."""
    url = getattr(config.get(), f"database_url_{login.lower()}").split("?", 1)[0] + "?dbname=postgres"
    settings_with(monkeypatch, **{f"DATABASE_URL_{login}": url})
    try:
        with pytest.raises(guard.NotATestDatabase, match="connected to database 'postgres'"):
            guard.require_test_database()
    finally:
        for made in db._engines.values():
            made.dispose()


def test_it_passes_on_a_truncated_database_with_no_worker(engine):
    with engine.begin() as conn:
        guard.require_no_other_worker(conn, "windtunnel:me:1")
        guard.require_quiet_database(conn)


def test_a_scheduler_tick_and_a_run_by_another_worker_gives_it_away(engine):
    """The finished queue.reap job no longer names its worker, but its unique key shows that one ticked."""
    with db.transaction(role="worker") as conn:
        scheduler.tick(conn, datetime.now(UTC))
    queue.run_once(lambda: db.transaction(role="worker"), "someone-else:7", ("default",))
    with engine.begin() as conn:
        with pytest.raises(guard.WorkerAttached):
            guard.require_no_other_worker(conn, "windtunnel:me:1")


def test_a_job_another_worker_has_claimed_gives_it_away(engine):
    with db.transaction(role="worker") as conn:
        queue.enqueue(conn, "x.y", {}, unique_key="x.y:1")
    with db.transaction(role="worker") as conn:
        assert queue.claim(conn, "someone-else:7") is not None
    with engine.begin() as conn:
        with pytest.raises(guard.WorkerAttached):
            guard.require_no_other_worker(conn, "windtunnel:me:1")
        guard.require_no_other_worker(conn, "someone-else:7")  # a job of the caller's own name is not another worker's


def test_an_old_reap_job_is_not_a_worker(engine):
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO queue_jobs (queue, kind, unique_key, status, created_at, finished_at)
                   VALUES ('default', 'queue.reap', 'queue.reap:1', 'succeeded', now() - interval '10 minutes', now())"""
            )
        )
        guard.require_no_other_worker(conn, "windtunnel:me:1")


@pytest.mark.parametrize("synthetic", [True, False])
def test_a_queued_job_of_a_real_person_makes_the_database_busy(engine, synthetic):
    with engine.begin() as conn:
        user = make_user(conn)
        conn.execute(text("UPDATE users SET is_synthetic = :s WHERE id = :u"), {"s": synthetic, "u": user})
        conn.execute(
            text("INSERT INTO queue_jobs (queue, kind, unique_key, user_id) VALUES ('default', 'x', 'k:1', :u)"),
            {"u": user},
        )
    with engine.begin() as conn:
        if synthetic:
            guard.require_quiet_database(conn)
        else:
            with pytest.raises(guard.DatabaseBusy):
                guard.require_quiet_database(conn)


def test_a_job_of_nobody_and_pending_mail_of_a_real_person_make_it_busy(engine):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO queue_jobs (queue, kind, unique_key) VALUES ('default', 'x', 'k:2')"))
    with engine.begin() as conn:
        with pytest.raises(guard.DatabaseBusy):
            guard.require_quiet_database(conn)
    empty(engine)
    with engine.begin() as conn:
        user = make_user(conn)
        conn.execute(
            text("INSERT INTO outbox (dedupe_key, user_id, template) VALUES ('brief:1', :u, 'brief_v1')"), {"u": user}
        )
    with engine.begin() as conn:
        with pytest.raises(guard.DatabaseBusy):
            guard.require_quiet_database(conn)
