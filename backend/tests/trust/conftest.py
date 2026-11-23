"""Fixtures shared by the privacy tests (plan 03-06): export, deletion and the registry tests.

Names are distinctive on purpose, so a fixture here never shadows one a test module defines for itself.
"""

from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app.platform import auth, config, db, queue, relay, sessions
from app.platform.sessions import Principal, current_user


@pytest.fixture
def make_person(engine) -> Callable[..., UUID]:
    """make_person("ada@mail.test"): an account row, committed, so the API (which has its own connections) sees it."""

    def make(email: str = "ada@mail.test") -> UUID:
        with engine.begin() as c:
            return c.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()

    return make


@pytest.fixture
def sign_in_as(api_client) -> Callable[[UUID], None]:
    """sign_in_as(user_id): the API client acts as this person from now on (the session dependency is overridden;
    one test below signs in for real with a code and a cookie)."""

    def sign_in(user_id: UUID) -> None:
        api_client.app.dependency_overrides[current_user] = lambda: Principal(user_id)

    return sign_in


@pytest.fixture
def emailed_code(api_client, engine) -> Callable[[str], str]:
    """emailed_code("ada@mail.test"): ask the API for a sign-in code for this address and read it as the person would
    from their mailbox (the relay is not involved: the sealed code is opened from the outbox row)."""
    auth._recent.clear()  # the per-address limit is kept in this process's memory and every test client shares one

    def issue(email: str) -> str:
        assert api_client.post("/v1/auth/codes", json={"email": email}).status_code == 202
        with engine.connect() as c:
            sealed = c.execute(
                text(
                    """SELECT o.payload->>'code_sealed' FROM outbox o
                       JOIN login_codes l ON l.id = CAST(o.payload->>'login_code_id' AS uuid)
                       WHERE lower(l.email) = :e ORDER BY o.created_at DESC LIMIT 1"""
                ),
                {"e": email.lower()},
            ).scalar_one()
        return sessions.open_sealed(sealed)

    return issue


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The erasure ledger, in a folder of the test's own. Nothing is written until an erasure runs."""
    path = tmp_path / "erasure" / "ledger.jsonl"
    monkeypatch.setenv("ERASURE_LEDGER_PATH", str(path))
    return path


@pytest.fixture
def mailbox(monkeypatch: pytest.MonkeyPatch) -> list[EmailMessage]:
    """Every message that would have gone to the mail server, as the list the test reads. The relay's send is replaced
    (its own tests talk to a real SMTP server); a message is built exactly as it would be sent."""
    sent: list[EmailMessage] = []

    def send(settings: config.Settings, message: EmailMessage) -> str:
        sent.append(message)
        return "sent"

    monkeypatch.setattr(relay, "_send", send)
    return sent


@pytest.fixture
def run_jobs() -> Callable[[], int]:
    """run_jobs(): run every queued job, as the worker would, and say how many ran. A job that fails is retried later,
    so it counts once and is not run again here. The handlers are the ones the worker process has: importing app.worker
    is what registers them, so a job the worker would not know is `unknown_kind` here too."""
    import app.worker  # noqa: F401

    def run() -> int:
        ran = 0
        while queue.run_once(lambda: db.transaction(role="worker"), "test-worker"):
            ran += 1
        return ran

    return run
