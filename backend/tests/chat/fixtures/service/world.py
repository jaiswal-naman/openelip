"""Shared pieces of the chat tool, service, route and stream tests (plan 05-04): an invented world with two candidates,
the real gateway on the fake provider with its ledger kept, and a reader for the event stream. Everything here is made
up (the candidates, the employer "Initech Demo", every address); no model is called.

The tools and the generation thread open their own connections as the API role, so what a test puts in the database is
committed; the tables are emptied after each test, and a reply that is still running is waited for first, so a thread
never outlives the test whose monkeypatches it runs under.

pytest keeps conftest.py off the import path, so the test files import these functions and wrap them in a fixture:
    from tests.chat.fixtures.service import world as chat_world
"""

import json
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.platform import config
from app.platform.llm import fake
from app.platform.sessions import Principal, current_user
from app.profile import service
from app.trust import api as trust
from tests.chat.fixtures import harness
from tests.match.factories import (
    LOCAL_DATE,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)
from tests.p5_factories import make_application

if TYPE_CHECKING:
    from app.chat import hub

RECORDINGS = Path(__file__).parent
YESTERDAY = LOCAL_DATE - timedelta(days=1)
WAIT_SECONDS = 20.0  # a reply that has not ended by then never will; the test fails instead of hanging


def use_provider(monkeypatch: pytest.MonkeyPatch, recording: Path | None = None, **env: str) -> None:
    """The real gateway on the settings of this test database with LLM_PROVIDER=fake (or what `env` says), answering
    from `recording` (a JSON list consumed in order, every task from the one file). Unlike the 05-03 harness the ledger
    is the real one: each provider request leaves an llm_calls row, which the usage and cost tests read."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    chosen = config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake", **env})
    monkeypatch.setattr(config, "get", lambda: chosen)
    monkeypatch.setattr(fake, "_source", recording)  # undone with the test
    monkeypatch.setattr(fake, "_given", {})


def reset_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty reply hub for this test: a reply of an earlier test is never seen by it. (Imported here, so the tests of
    the tools, which run no reply, do not need the hub.)"""
    from app.chat import hub

    monkeypatch.setattr(hub, "_replies", {})


def make_world(engine: Engine, *, consent: bool = True) -> SimpleNamespace:
    """Ada and Bob, committed. Ada: the factory record (India, remote or hybrid), a constraint never to work for Globex
    Demo, a target-title preference, one shown decision with its evidence (on `ok`), one the Globex constraint
    filtered (on `blocked`) and one application. Bob: a record, a shown decision on `ok` and a withheld one on
    `blocked`. Five postings: `ok` passes Ada's constraints and has a decision; `abroad` (country) and `onsite`
    (arrangement) break them and have none; `blocked` breaks the Globex one; `fine` passes them all and has no
    decision. Call after use_provider: the consent is given under the wording of the providers in force."""
    w = SimpleNamespace()
    with engine.begin() as c:
        w.ada, w.bob = make_user(c), make_user(c)
        w.record, w.bobs_record = make_record(c, w.ada), make_record(c, w.bob)
        w.excluded = service.set_constraint(
            c, w.ada, "excluded_company", {"company_names": ["Globex Demo"]}, "Never Globex Demo."
        ).row["id"]
        service.add_preference(c, w.ada, "target_title", "Data engineer roles.", value="data engineer", weight=2)
        w.ok = make_job(
            c,
            title="Data engineer",
            description="Build nightly pipelines with Python and SQL.",
            requirements=[("Python", "required"), ("SQL", "preferred")],
            country="IN",
            arrangements=("remote",),
        )
        w.abroad = make_job(
            c,
            title="Data engineer",
            description="Build pipelines on site in Austin.",
            requirements=[("Build pipelines", "required")],
            company_name="Hooli Demo",
            country="US",
            arrangements=("onsite",),
        )
        w.blocked = make_job(
            c,
            title="Data engineer",
            description="Build pipelines for Globex.",
            requirements=[("Build pipelines", "required")],
            company_name="Globex Demo",
            country="IN",
            arrangements=("remote",),
        )
        w.onsite = make_job(
            c,
            title="Data engineer",
            description="Build pipelines in an office.",
            requirements=[("Build pipelines", "required")],
            company_name="Umbrella Demo",
            country="IN",
            arrangements=("onsite",),
        )
        w.fine = make_job(
            c,
            title="Data analyst",
            description="Analyse data for the finance team.",
            requirements=[("Analyse data", "required")],
            country="IN",
            arrangements=("hybrid",),
        )
        w.shown = make_shown_decision(
            c,
            make_run(c, w.ada, local_date=LOCAL_DATE),
            w.ada,
            w.ok,
            [Verdict("met", w.record.fact_ids[1:2]), Verdict("no_evidence")],
            summary="A fit, with the evidence below.",
        )
        w.filtered = make_decision(
            c,
            make_run(c, w.ada, local_date=YESTERDAY),
            w.ada,
            w.blocked,
            outcome="filtered",
            reasons=("hard_constraint",),
            violated_kinds=("excluded_company",),
        )
        bobs_run = make_run(c, w.bob, local_date=LOCAL_DATE)
        w.bobs_shown = make_shown_decision(
            c,
            bobs_run,
            w.bob,
            w.ok,
            [Verdict("met", w.bobs_record.fact_ids[1:2]), Verdict("no_evidence")],
            summary="Bob's own line.",
        )
        w.bobs_withheld = make_decision(
            c,
            make_run(c, w.bob, local_date=YESTERDAY),
            w.bob,
            w.blocked,
            outcome="withheld",
            reasons=("not_scored",),
        )
        w.application = make_application(c, w.ada, w.ok)
        if consent:
            for person in (w.ada, w.bob):
                trust.grant_consent(c, person, "ai_processing", trust.ai_processing_text_version())
    return w


def start(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, recording: Path | None = None, **env: str
) -> SimpleNamespace:
    """The provider first (the consent names it), then the world."""
    harness.empty_tables(engine)
    use_provider(monkeypatch, recording, **env)
    return make_world(engine)


def finish(engine: Engine, *, empty: bool = False) -> None:
    """Wait for every reply that is still running, then (when the test has no API client to do it) empty the tables."""
    from app.chat import hub

    for reply in hub.running():
        reply.finished.wait(WAIT_SECONDS)
    if empty:
        harness.empty_tables(engine)


def sign_in(client: Any, user: UUID) -> None:
    """The route's person is `user` from now on (the session dependency is replaced, as the match route tests do)."""
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


def wait_done(message_id: UUID | str) -> "hub.Reply":
    """Block (on an event, never a sleep) until the reply has ended and been stored."""
    from app.chat import hub

    reply = hub.get(UUID(str(message_id)))
    assert reply is not None, "the reply is not in the hub"
    assert reply.finished.wait(WAIT_SECONDS), "the reply did not end"
    return reply


def frames(raw: str) -> list[dict[str, Any]]:
    """The frames of an event-stream body, in order. {"retry": 3000}, {"comment": "ping"}, or an event with its `id`,
    `event` and `data` (JSON, decoded). A frame is what comes before a blank line."""
    found: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, Any] = {}
        for line in block.split("\n"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if not field:
                frame["comment"] = value
            elif field == "retry":
                frame["retry"] = int(value)
            elif field == "id":
                frame["id"] = int(value)
            elif field == "event":
                frame["event"] = value
            elif field == "data":
                frame["data"] = json.loads(value)
        found.append(frame)
    return found


def count(engine: Engine, source: str, **params: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {source}"), params).scalar_one()
