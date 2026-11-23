"""Pieces the brief tests share (plan 04-05): scripted answers for the apply-link check, and a signed-in person.

pytest keeps conftest.py off the import path, so the tests reach these through fixtures. Everything is invented: people
are made up, addresses are mail.test, jobs are "Initech Demo" postings from the 04-01 factories.
"""

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.ingest import liveness
from app.platform import config, http
from app.platform.sessions import Principal, current_user
from tests.platform.test_relay import FakeSmtp


class ApplyLinks:
    """Stands in for fetch_public, the one network call of the morning re-check: answers the status set for a URL (200
    for any other) or raises the error set for it, and remembers every URL it was asked for. The real verify_live
    runs on top of it, so the jobs table changes exactly as it would."""

    def __init__(self) -> None:
        self.answers: dict[str, int | Exception] = {}
        self.asked: list[str] = []

    @staticmethod
    def url(external_id: str) -> str:
        """The apply URL the 04-01 factory gives a job made with this external_id."""
        return f"https://example.test/apply/{external_id}"

    def __call__(self, url: str) -> http.PublicResponse:
        self.asked.append(url)
        answer = self.answers.get(url, 200)
        if isinstance(answer, Exception):
            raise answer
        return http.PublicResponse(answer, url, "text/html", b"")


@pytest.fixture
def apply_links(monkeypatch: pytest.MonkeyPatch) -> ApplyLinks:
    links = ApplyLinks()
    monkeypatch.setattr(liveness, "fetch_public", links)
    return links


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch, api_client: Any) -> Iterator[FakeSmtp]:
    """A small real SMTP server (test_relay.py's) for the relay to send to; api_client empties the tables around it."""
    server = FakeSmtp()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = replace(config.get(), smtp_url=server.url(), mail_from="no-reply@careers.test")
    monkeypatch.setattr(config, "get", lambda: settings)
    yield server
    server.shutdown()
    server.server_close()


def new_person(engine: Engine, email: str = "ada@mail.test") -> UUID:
    """A committed account, for the tests that go through the API or the real database roles."""
    with engine.begin() as c:
        return c.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


@pytest.fixture
def act_as() -> Callable[[Any, UUID], None]:
    """act_as(client, user_id): the API client acts as this person from now on (signed in, without the code)."""

    def switch(client: Any, user_id: UUID) -> None:
        client.app.dependency_overrides[current_user] = lambda: Principal(user_id)

    return switch
