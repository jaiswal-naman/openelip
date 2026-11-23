"""Shared pieces of the match run tests (plan 04-03): an invented candidate, invented postings, the real gateway on the
fake provider, and the few reads of stored rows every test makes. See README.md: everything here is made up.

The run opens its own connections (the match role and the worker), so what a test puts in the database must be
committed, and the tables are emptied around each test the way the API tests do it.

pytest keeps conftest.py off the import path and nobody else owns a conftest in this folder, so the three run test
files import these functions and each wraps them in a small fixture of its own.
"""

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, text

from app.match import retrieval
from app.platform import config
from app.platform.llm import fake, gateway, registry
from app.platform.llm.embed import EmbeddingUnavailable
from app.trust import api as trust
from tests.match import factories

RECORDINGS = Path(__file__).parent
LOCAL_DATE = factories.LOCAL_DATE
DEFAULT_REQUIREMENTS = (("Python", "required"), ("SQL", "required"))
EMPTY = text("TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls RESTART IDENTITY CASCADE")


def empty_tables(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(EMPTY)


def use_provider(monkeypatch: pytest.MonkeyPatch, recording: str | None = None, **env: str) -> list[tuple[Any, ...]]:
    """The real gateway on the settings of this test database with LLM_PROVIDER=fake (or what `env` says), answering
    from the recording in this folder. The ledger and registry writes are replaced by the list this returns, as
    (task, prompt_version, attempt, outcome) per provider request. Nothing here reaches a model."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    chosen = config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake", **env})
    monkeypatch.setattr(config, "get", lambda: chosen)
    calls: list[tuple[Any, ...]] = []

    def record(route, model_id, prompt_version, principal, attempt, outcome, reply, started):  # type: ignore[no-untyped-def]
        calls.append((route.task, prompt_version, attempt, outcome))

    monkeypatch.setattr(registry, "model_id", lambda route: UUID(int=1))
    monkeypatch.setattr(gateway, "_record", record)
    monkeypatch.setattr(fake, "_source", RECORDINGS / recording if recording else None)  # undone with the test
    monkeypatch.setattr(fake, "_given", {})
    return calls


def no_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local embedding model is not available, so retrieval is by keywords (and nothing is downloaded or loaded)."""

    def unavailable(texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable("test")

    monkeypatch.setattr(retrieval, "embed_passages", unavailable)


def add_person(
    engine: Engine,
    *,
    display_name: str | None = None,
    consent: bool = True,
    skill: bool = True,
    constraints: bool = True,
) -> UUID:
    """An invented candidate with the factory's confirmed record (India, remote or hybrid; none of it with
    constraints=False), the skill Python as a fourth fact, and the ai_processing consent under the wording of the
    provider in force (so call use_provider first)."""
    with engine.begin() as c:
        user = factories.make_user(c)
        factories.make_record(c, user)
        if not constraints:
            drop_constraints(c, user)
        if skill:
            event = c.execute(
                text(
                    """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source,
                                                   record_version_after)
                       VALUES (:u, 'applied', 'fact', 'create', CAST(:after AS jsonb), 'candidate_edit', 1)
                       RETURNING id"""
                ),
                {"u": user, "after": json.dumps({"kind": "skill"})},
            ).scalar_one()
            c.execute(
                text(
                    """INSERT INTO profile_facts (user_id, kind, statement, source, profile_event_id, confirmed_at)
                       VALUES (:u, 'skill', 'Python', 'candidate_edit', :e, now())"""
                ),
                {"u": user, "e": event},
            )
        if display_name is not None:
            c.execute(text("UPDATE users SET display_name = :n WHERE id = :u"), {"n": display_name, "u": user})
        if consent:
            trust.grant_consent(c, user, "ai_processing", trust.ai_processing_text_version())
    return user


def add_constraint(conn: Connection, user: UUID, kind: str, **fields: Any) -> None:
    """One active hard constraint of `kind` (fields: country_codes, cities, arrangements, employment_types, seniorities,
    salary_floor, salary_currency, company_names), with the change-log row every record row points at."""
    event = conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', 'constraint', 'create', CAST(:after AS jsonb), 'candidate_edit', 1)
               RETURNING id"""
        ),
        {"u": user, "after": json.dumps({"kind": kind})},
    ).scalar_one()
    columns = ("country_codes", "cities", "arrangements", "employment_types", "seniorities", "company_names")
    conn.execute(
        text(
            """INSERT INTO constraints (user_id, kind, statement, country_codes, cities, arrangements, employment_types,
                                        seniorities, salary_floor, salary_currency, company_names, source,
                                        profile_event_id)
               VALUES (:u, CAST(:kind AS constraint_kind), 'An invented constraint.',
                       CAST(:country_codes AS char(2)[]), CAST(:cities AS text[]),
                       CAST(:arrangements AS work_arrangement[]), CAST(:employment_types AS employment_type[]),
                       CAST(:seniorities AS seniority[]), :salary_floor, :salary_currency,
                       CAST(:company_names AS text[]), 'candidate_edit', :e)"""
        ),
        {
            "u": user,
            "kind": kind,
            "e": event,
            "salary_floor": fields.get("salary_floor"),
            "salary_currency": fields.get("salary_currency"),
            **{name: fields.get(name) for name in columns},
        },
    )


def drop_constraints(conn: Connection, user: UUID) -> None:
    """Retire every active hard constraint of the person (the record's lifecycle column, not a delete)."""
    conn.execute(
        text("UPDATE constraints SET superseded_at = now() WHERE user_id = :u AND superseded_at IS NULL"), {"u": user}
    )


def add_job(
    engine: Engine,
    name: str,
    *,
    requirements: tuple[tuple[str, str], ...] = DEFAULT_REQUIREMENTS,
    **fields: Any,
) -> factories.Job:
    """A committed, open, remote posting in India whose requirements are `requirements` ((text, importance) pairs)."""
    sentences = [f"You need {words}." for words, _ in requirements] or ["Python is used here."]  # still found
    description = f"{name} at Initech Demo. " + " ".join(sentences)
    fields = {"country": "IN", "arrangements": ("remote",), **fields}
    with engine.begin() as c:
        return factories.make_job(c, title=name, description=description, requirements=requirements, **fields)


def add_requirements(engine: Engine, job_id: int, version: int, requirements: tuple[tuple[str, str], ...]) -> list[int]:
    """What the extraction sweep does for a posting at `version`: its requirements as rows, and requirements_version.
    Each text must occur in the posting's current description (the database checks the span)."""
    ids = []
    with engine.begin() as c:
        description = c.execute(text("SELECT description FROM jobs WHERE id = :j"), {"j": job_id}).scalar_one()
        for ordinal, (words, importance) in enumerate(requirements, start=1):
            start = description.index(words)
            ids.append(
                c.execute(
                    text(
                        """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                         prompt_version)
                           VALUES (:j, :v, :o, CAST(:i AS requirement_importance), 'skill', :t,
                                   int4range(:s, :e), 'test-requirements-v1')
                           RETURNING id"""
                    ),
                    {
                        "j": job_id,
                        "v": version,
                        "o": ordinal,
                        "i": importance,
                        "t": words,
                        "s": start,
                        "e": start + len(words),
                    },
                ).scalar_one()
            )
        c.execute(text("UPDATE jobs SET requirements_version = :v WHERE id = :j"), {"v": version, "j": job_id})
    return ids


def fetch(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), params))
