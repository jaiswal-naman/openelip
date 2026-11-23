"""Shared set-up of the Phase 5 gold-set suite tests (plan 05-06): the application's settings and the eval seed look at
the private database of tests/evals/conftest.py, under all four roles (the conftest names three; the matching cases of
the injection suite read as the match role), with the fake provider. Imported as `from tests.evals import p5_env`.

The seed reads DATABASE_URL_MIGRATE from the environment, as it does in CI and in a local run, so the variables are set
for the test; monkeypatch puts them back.
"""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url

from app.evals import EVALS
from app.platform import config

ROLE_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER", "DATABASE_URL_MATCH")
ROOT = EVALS.parent


def use_private_database(monkeypatch, evals_db: URL) -> None:
    """All four DATABASE_URL_* name the private database; the settings carry them, with LLM_PROVIDER=fake. Call it
    right after the `engine` fixture, before anything opens a connection through app.platform.db."""
    urls = {
        name: make_url(os.environ[name]).set(database=evals_db.database).render_as_string(hide_password=False)
        for name in ROLE_VARIABLES
    }
    for name, url in urls.items():
        monkeypatch.setenv(name, url)
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)


def load(name: str, path: Path):
    """A script of evals/ by a name of its own: every suite has a score.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prompt_hash(path: Path) -> str:
    """The 12-character content hash the runner and the prompt modules use."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()[:12]


def run_row(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT suite::text AS suite, status::text AS status, config, metrics, is_gate_passed "
                    "FROM eval_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
