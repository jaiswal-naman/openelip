"""Fixtures for the requirement tests: the real gateway and the real fake provider, without a database.

The gateway writes one ledger row per provider request and one registry row per model. The database is not what these
tests are about (the gateway's own tests cover it), so those two writes are replaced by a list the fixture hands back.
Everything else runs for real: routing, the fake provider's replay of a recorded file, and schema validation.
"""

from pathlib import Path
from uuid import UUID

import pytest

from app.platform import config
from app.platform.llm import fake, gateway, registry

RECORDED = Path(__file__).parent / "recorded"


def settings(**env: str) -> config.Settings:
    return config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **env})


@pytest.fixture
def use_settings(monkeypatch: pytest.MonkeyPatch):
    """use_settings(LLM_PROVIDER="anthropic", ...): run the gateway under these settings (default: the fake)."""

    def use(**env: str) -> None:
        chosen = settings(**env)
        monkeypatch.setattr(config, "get", lambda: chosen)

    use()
    return use


@pytest.fixture
def gateway_calls(monkeypatch: pytest.MonkeyPatch, use_settings):
    """The provider requests the gateway makes, as (task, prompt_version, principal, attempt, outcome)."""
    calls: list[tuple] = []

    def record(route, model_id, prompt_version, principal, attempt, outcome, reply, started):
        calls.append((route.task, prompt_version, principal, attempt, outcome))

    monkeypatch.setattr(registry, "model_id", lambda route: UUID(int=1))
    monkeypatch.setattr(gateway, "_record", record)
    yield calls
    fake.replay_from(None)


@pytest.fixture
def replay():
    """replay("name.json"): the fake provider answers from recorded/name.json, in order."""

    def use(name: str) -> None:
        fake.replay_from(RECORDED / name)

    return use
