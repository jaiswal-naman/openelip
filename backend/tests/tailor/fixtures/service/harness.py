"""Shared pieces of the plan 05-05 tests (tailoring service, routes and packets): the real gateway on the fake provider,
recordings written from the answers a test shows, and one invented world to run them in. Everything is invented (Ada
Example, "Initech Demo", addresses at example.test); no model is called.

pytest keeps conftest.py off the import path and plan 05-05 owns none, so the test files import these functions and each
wraps them in a small fixture of its own.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text

from app.platform import config
from app.platform.llm import fake, gateway, registry
from app.platform.sessions import Principal, current_user
from app.profile import api as profile
from app.trust import api as trust
from tests.p5_factories import make_job, make_tailorable_record, make_user

EMPTY = text("TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls, idempotency_keys RESTART IDENTITY CASCADE")
JOB_TEXT = "Build reporting pipelines in Python. Experience with PostgreSQL. Kubernetes is a plus."
REQUIREMENTS = (("Build reporting pipelines in Python", "required"), ("Experience with PostgreSQL", "required"))
NO_RECORDINGS = Path(__file__).parent / "no-recordings"  # never created: every task finds no file, so AIDisabled


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
    if recording is None:  # "no recording" must stay true now that recorded/ ships the demo's default answers (05-07)
        monkeypatch.setattr(fake, "_RECORDED", NO_RECORDINGS)
    return calls


def write_answers(directory: Path, *answers: Any, name: str = "answers.json") -> Path:
    """A recording of these answers, in the order they will be given. A string is the model's text verbatim (script an
    invalid answer with one); anything else is an answer, dumped to JSON."""
    path = directory / name
    path.write_text(json.dumps(list(answers)), encoding="utf-8")
    return path


def edit(
    op: str,
    *,
    target: str | None = None,
    after: str = "",
    supporting: tuple[str, ...] = (),
    order=(),
    section: str = "experience",
) -> dict[str, Any]:
    """One tailor.propose edit as the model returns it (aliases f1..fN as the request numbers them)."""
    return {
        "op": op,
        "target": target,
        "section": section,
        "after_text": after,
        "supporting": list(supporting),
        "requirements": [],
        "order": list(order),
    }


def world(engine: Engine) -> SimpleNamespace:
    """Ada with a tailorable record and the AI processing consent, an open job, and Bob with a record of his own.
    `alias` maps a fact name (role_initech, skill_python ...) to the alias f1..fN the request gives it,
    which is its place in profile.api.usable_facts."""
    w = SimpleNamespace()
    with engine.begin() as c:
        w.ada, w.bob = make_user(c), make_user(c)
        w.record = make_tailorable_record(c, w.ada)
        make_tailorable_record(c, w.bob)
        w.job = make_job(c, title="Data engineer", description=JOB_TEXT, requirements=REQUIREMENTS, country="IN")
        for person in (w.ada, w.bob):
            trust.grant_consent(c, person, "ai_processing", trust.ai_processing_text_version())
        order = [row["id"] for row in profile.usable_facts(c, w.ada)]
    ids = {str(v): name for name, v in w.record.facts.items()}
    w.alias = {ids[fid]: f"f{n}" for n, fid in enumerate(order, 1)}
    return w


def sign_in(client: Any, user: UUID) -> None:
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


def key() -> dict[str, str]:
    """A fresh Idempotency-Key header."""
    return {"Idempotency-Key": uuid4().hex}
