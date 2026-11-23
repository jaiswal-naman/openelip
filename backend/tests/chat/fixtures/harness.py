"""Shared pieces of the chat tests (plan 05-03): the real gateway on the fake provider, and recordings written from the
answers a test shows. Everything is invented; no model is called.

pytest keeps conftest.py off the import path and nobody owns one in this folder, so the chat test files import these
functions (`from tests.chat.fixtures import harness`) and each wraps them in a small fixture of its own.
"""

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.platform import config
from app.platform.llm import fake, gateway, registry

FIXTURES = Path(__file__).parent
EMPTY = text("TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls RESTART IDENTITY CASCADE")


def empty_tables(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(EMPTY)


def use_provider(monkeypatch: pytest.MonkeyPatch, recording: Path | None = None, **env: str) -> list[tuple[Any, ...]]:
    """The real gateway on the settings of this test database with LLM_PROVIDER=fake (or what `env` says), answering
    from `recording` (a JSON list consumed in order, every task from the one file). The ledger and registry writes are
    replaced by the list this returns, as (task, prompt_version, attempt, outcome) per provider request."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    chosen = config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake", **env})
    monkeypatch.setattr(config, "get", lambda: chosen)
    calls: list[tuple[Any, ...]] = []

    def record(route, model_id, prompt_version, principal, attempt, outcome, reply, started):  # type: ignore[no-untyped-def]
        calls.append((route.task, prompt_version, attempt, outcome))

    monkeypatch.setattr(registry, "model_id", lambda route: UUID(int=1))
    monkeypatch.setattr(gateway, "_record", record)
    monkeypatch.setattr(fake, "_source", recording)  # undone with the test
    monkeypatch.setattr(fake, "_given", {})
    return calls


def write_answers(directory: Path, *answers: Any, name: str = "answers.json") -> Path:
    """A recording of these answers, in the order they will be given. A string is the model's text verbatim (script an
    invalid answer with one); anything else is an answer, dumped to JSON."""
    path = directory / name
    path.write_text(json.dumps(list(answers)), encoding="utf-8")
    return path
