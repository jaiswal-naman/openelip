"""A database for the eval tests alone.

The runner and the gateway commit on connections of their own, so these tests cannot use the rolled-back `conn`
fixture, and other test modules truncate the shared test database. The process id keeps two test runs apart.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.platform import config, db
from app.platform.llm import fake

DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")


@pytest.fixture(scope="module")
def evals_db(make_database, alembic_run) -> URL:
    url: URL = make_database(f"app_evals_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def engine(evals_db: URL, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A superuser engine on the private database (empty tables for every test). The application's own settings and
    engines point at the same database, with the fake provider chosen."""
    admin = create_engine(evals_db.set(drivername="postgresql+psycopg"))
    with admin.begin() as conn:
        conn.execute(text("TRUNCATE eval_runs, eval_items, llm_calls, model_registry RESTART IDENTITY CASCADE"))
    urls = {
        name: make_url(os.environ[name]).set(database=evals_db.database).render_as_string(hide_password=False)
        for name in DB_VARIABLES
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    yield admin
    for application_engine in db._engines.values():
        application_engine.dispose()
    fake.replay_from(None)
    admin.dispose()
