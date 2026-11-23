"""Test harness (PLT-08). Tests run against a database whose name ends in _test, never against the real one.

The session fixture creates that database through the bootstrap login when it is missing and migrates it to head.
The application's own database settings are pointed at it before anything imports them, so the API under test
connects as the real app_api role and row-level security applies.
"""

import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
DEFAULT_TEST_URL = "postgresql://app:app_local_only@127.0.0.1:5433/app_test"
# Everything the application writes or the tests insert; the job index is included because tests commit fixture rows.
DATA_TABLES = (
    "users, sessions, login_codes, idempotency_keys, outbox, queue_jobs, llm_calls, model_registry, local_model_key, "
    "eval_runs, eval_items, fetch_runs, jobs, job_sources, "
    "match_runs, match_decisions, match_evidence, feedback_events, briefs, brief_items, "
    "tailored_resumes, resume_edits, application_packets, applications, application_events, "
    "chat_sessions, chat_messages, "
    "organizations, org_members, roles, role_requirements, introductions, shortlist_entries, interest_signals, "
    "campaigns, campaign_steps, company_dossiers, agent_runs, agent_actions, approvals, autonomy_settings, "
    "guardian_checks, sim_results, sim_runs, sim_scenarios, "
    "twin_scopes, twin_sessions, twin_messages, agent_tokens, "
    "langgraph.checkpoints, langgraph.checkpoint_blobs, langgraph.checkpoint_writes"
)


def assert_test_database(url: URL) -> None:
    """Refuse any database whose name does not end in _test (threat T-01-05)."""
    if not re.fullmatch(r"[a-z0-9_]+_test", url.database or ""):
        raise RuntimeError(f"refusing to use database {url.database!r}: a test database name must end in _test")


def _as_login(url: URL, role: str, password_variable: str, development_default: str) -> str:
    password = os.environ.get(password_variable) or development_default
    return url.set(username=role, password=password).render_as_string(hide_password=False)


TEST_URL = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL))
assert_test_database(TEST_URL)
# Forced, not defaulted: a developer's shell may point these at the real database.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL_MIGRATE"] = TEST_URL.render_as_string(hide_password=False)
os.environ["DATABASE_URL_API"] = _as_login(TEST_URL, "app_api", "APP_DB_API_PASSWORD", "app_api_local_only")
os.environ["DATABASE_URL_WORKER"] = _as_login(TEST_URL, "app_worker", "APP_DB_WORKER_PASSWORD", "app_worker_local_only")
os.environ["DATABASE_URL_MATCH"] = _as_login(TEST_URL, "app_match", "APP_DB_MATCH_PASSWORD", "app_match_local_only")


def _sa_url(url: URL) -> URL:
    return url.set(drivername="postgresql+psycopg")


def _admin_engine(url: URL) -> Engine:
    return create_engine(_sa_url(url).set(database="postgres"), isolation_level="AUTOCOMMIT")


def create_database(url: URL, *, replace: bool = False) -> None:
    assert_test_database(url)
    admin = _admin_engine(url)
    with admin.connect() as c:
        if replace:
            c.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar():
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


def drop_database(url: URL) -> None:
    assert_test_database(url)
    admin = _admin_engine(url)
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


def alembic_config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    config.attributes["url"] = url  # read by db/env.py
    return config


@pytest.fixture(scope="session")
def assert_test_db() -> Callable[[URL], None]:
    """The guard itself, so a test can prove it refuses what it should."""
    return assert_test_database


@pytest.fixture(scope="session")
def alembic_run() -> Callable[[str, URL, str], None]:
    """alembic_run("upgrade", url, "head"): run an Alembic command against a test database."""

    def run(action: str, url: URL, revision: str) -> None:
        getattr(command, action)(alembic_config(url), revision)

    return run


@pytest.fixture(scope="session")
def make_database() -> Iterator[Callable[[str], URL]]:
    """make_database("name_test"): a new, empty database next to the main test one, dropped at session end."""
    created: list[URL] = []

    def make(name: str) -> URL:
        # Prefixed with the main test database's name, so two test runs on one server (each with its own
        # TEST_DATABASE_URL) never create or force-drop each other's scratch databases.
        url = TEST_URL.set(database=f"{(TEST_URL.database or '').removesuffix('_test')}_{name}")
        create_database(url, replace=True)
        created.append(url)
        return url

    yield make
    for url in created:
        drop_database(url)


@pytest.fixture(scope="session")
def database_url() -> URL:
    """The main test database, created if missing and migrated to head."""
    create_database(TEST_URL)
    command.upgrade(alembic_config(TEST_URL), "head")
    return TEST_URL


@pytest.fixture(scope="session")
def engine(database_url: URL) -> Iterator[Engine]:
    """A bootstrap (superuser) engine on the test database; it bypasses row-level security."""
    eng = create_engine(_sa_url(database_url))
    with eng.connect() as c:
        assert (c.execute(text("SELECT current_database()")).scalar() or "").endswith("_test")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is rolled back after the test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _empty_tables(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def api_client(engine: Engine) -> Iterator["TestClient"]:
    """The API from app.main.create_app() on the test database. The API opens its own connections, so what it
    commits stays; the tables are emptied before and after each test instead of rolled back."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    _empty_tables(engine)
    with TestClient(create_app()) as client:
        yield client
    _empty_tables(engine)
