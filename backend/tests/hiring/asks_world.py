"""Shared pieces of the ask-pipeline tests (plan 06-05): the committed-data fixtures the role run needs, the fake
provider answering from tests/match/fixtures/role_run, and a few reads. Everything is invented (factories.py).

The role run opens its own connections (the match role and the worker), so what a test builds must be committed, and the
tables are emptied around each test. A test module wraps `committed` and `provider` in fixtures of its own.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.platform import db
from tests.hiring import factories as hiring
from tests.match.fixtures.run import world

RECORDINGS = Path(__file__).parents[1] / "match" / "fixtures" / "role_run"
EMPTY = text(
    "TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls, eval_runs, organizations, outbox "
    "RESTART IDENTITY CASCADE"
)


def committed(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The body of each test module's `committed` fixture (a module defines the fixture, so no name is redefined)."""

    def empty() -> None:
        with engine.begin() as c:
            c.execute(EMPTY)

    empty()
    world.no_embedder(monkeypatch)
    yield
    empty()


def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[tuple[Any, ...]]]:
    """The body of each module's `provider` fixture. provider("all_met.json", LLM_PROVIDER=...) puts the gateway on the
    fake provider answering from the role_run recordings and returns the list of provider requests."""

    def use(recording: str = "all_met.json", **env: str) -> list[tuple[Any, ...]]:
        calls = world.use_provider(monkeypatch, recording, **env)
        monkeypatch.setattr(world.fake, "_source", RECORDINGS / recording)
        return calls

    return use


def make_role(engine: Engine, *, org: hiring.Org | None = None, **fields: Any) -> hiring.Role:
    with engine.begin() as c:
        return hiring.make_open_role(c, org or hiring.make_org(c), **fields)


def make_candidate(engine: Engine, **fields: Any) -> hiring.Candidate:
    with engine.begin() as c:
        return hiring.make_candidate(c, **fields)


def worker(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run fn(conn, ...) on a worker connection that commits, the way a queue handler does."""
    with db.transaction(role="worker") as conn:
        return fn(conn, *args, **kwargs)


def introductions(engine: Engine, role: hiring.Role) -> list[Any]:
    return world.fetch(
        engine,
        """SELECT i.id, i.user_id, i.status::text AS status, i.share_preview, i.asked_at, i.expires_at,
                  i.match_decision_id FROM introductions i WHERE i.role_id = :r ORDER BY i.user_id""",
        r=role.id,
    )


def role_runs(engine: Engine, role: hiring.Role) -> list[Any]:
    return world.fetch(
        engine,
        "SELECT id, status::text AS status, evidence_prompt_version FROM match_runs "
        "WHERE role_id = :r ORDER BY started_at",
        r=role.id,
    )


def open_role_of(engine: Engine, role_id: UUID) -> Any:
    from app.hiring import asks

    return worker(asks.open_role, role_id)
