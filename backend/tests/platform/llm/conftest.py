"""Fixtures for the gateway tests.

The gateway commits its ledger rows on connections of its own, so these tests cannot use the rolled-back `conn`
fixture. Other test modules truncate the shared test database, so these tests get a database that only they use.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.platform import config, db
from app.platform.llm import fake, registry

DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")
TABLES = "llm_calls, model_registry, users"

# A fake provider that is not local, so a price is allowed: the arithmetic of the ledger can be asserted.
PRICED_FAKE_TOML = """
[providers.fake]
label = "Recorded responses"
adapter = "fake"
retention_note = "No model is called."

[tasks.ping]
description = "Test task."
data_class = "synthetic"
max_tokens = 64
timeout_seconds = 5

[tasks.ping.models.fake]
model = "recorded"
input_usd_per_mtok = {input_usd}
output_usd_per_mtok = {output_usd}
price_checked_at = 2026-11-22
"""


class Harness:
    """What a gateway test needs: settings, a models.toml, scripted answers and a look at the ledger."""

    def __init__(self, url: URL, engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.url = url
        self.engine = engine
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path

    def configure(self, **env: str) -> config.Settings:
        """Install the settings for this test. They start from nothing, so a real key in the developer's shell never
        reaches a test; the database variables point at this test's own database."""
        urls = {
            name: make_url(os.environ[name]).set(database=self.url.database).render_as_string(hide_password=False)
            for name in DB_VARIABLES
        }
        settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls, **env})
        self._monkeypatch.setattr(config, "get", lambda: settings)
        return settings

    def toml(self, body: str) -> None:
        """Use this text as config/models.toml for the rest of the test."""
        path = self._tmp / "models.toml"
        path.write_text(body, encoding="utf-8")
        self._monkeypatch.setattr(registry, "PATH", path)

    def use_priced_fake(self, input_usd: float = 2.5, output_usd: float = 10.0) -> None:
        self.toml(PRICED_FAKE_TOML.format(input_usd=input_usd, output_usd=output_usd))

    def record(self, *answers: Any) -> None:
        """Script the fake provider: these answers, in order, for every task."""
        path = self._tmp / "recorded.json"
        path.write_text(json.dumps(list(answers)), encoding="utf-8")
        fake.replay_from(path)

    def user(self, email: str = "a@example.test") -> UUID:
        with self.engine.begin() as conn:
            found = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar()
            if found:
                return found  # type: ignore[no-any-return]
            return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()

    def ledger(self) -> list[dict[str, Any]]:
        sql = text(
            "SELECT l.task, l.prompt_version, l.outcome::text AS outcome, l.user_id, l.input_tokens, l.output_tokens, "
            "l.cache_read_tokens, l.cache_write_tokens, l.cost_microusd, l.latency_ms, l.attempt, "
            "m.provider, m.model_name FROM llm_calls l JOIN model_registry m ON m.id = l.model_id ORDER BY l.id"
        )
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(sql).mappings()]

    def everything_stored(self) -> str:
        """Every column of every ledger and registry row, as one string: what a canary must not appear in."""
        sql = text(
            "SELECT coalesce((SELECT string_agg(row_to_json(l)::text, ' ') FROM llm_calls l), '') || "
            "coalesce((SELECT string_agg(row_to_json(m)::text, ' ') FROM model_registry m), '')"
        )
        with self.engine.connect() as conn:
            return conn.execute(sql).scalar_one()  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def llm_db(make_database: Any, alembic_run: Any) -> URL:
    """A migrated database used by these tests alone. The process id keeps two test runs from dropping each other's."""
    url: URL = make_database(f"llm_gateway_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def harness(llm_db: URL, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    engine = create_engine(llm_db.set(drivername="postgresql+psycopg"))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    monkeypatch.setattr(db, "_engines", {})  # the application's engines are built from the settings: this test's own
    helper = Harness(llm_db, engine, monkeypatch, tmp_path)
    helper.configure()
    yield helper
    for application_engine in db._engines.values():
        application_engine.dispose()
    fake.replay_from(None)
    engine.dispose()
