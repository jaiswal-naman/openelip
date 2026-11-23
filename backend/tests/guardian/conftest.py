"""Guardian tests: the registries are filled the way the API and the worker fill them (by importing app.campaigns), and
a test that commits rows (the real-role tests do) leaves the tables empty before and after."""

import importlib

import pytest
from sqlalchemy import Engine, text

from app.guardian import checks, policy
from tests.conftest import DATA_TABLES


@pytest.fixture(autouse=True)
def registries():
    """The campaign gate and citation check registered, as they are in both processes; restored after the test."""
    campaigns = importlib.import_module("app.campaigns")
    importlib.reload(importlib.import_module("app.campaigns.gate"))
    before = (dict(policy._readers), checks._citation_check)
    yield campaigns
    policy._readers.clear()
    policy._readers.update(before[0])
    checks._citation_check = before[1]


@pytest.fixture
def clean(engine: Engine):
    """Empty tables around a test that commits (the real-role tests: db.transaction opens its own connections)."""
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def smtp(monkeypatch):
    """A small real SMTP server on a local port (the Phase 1 relay test's), set as the relay's mail server."""
    import threading
    from dataclasses import replace

    from app.platform import auth, config
    from tests.platform.test_relay import FakeSmtp

    server = FakeSmtp()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = replace(config.get(), smtp_url=server.url(), mail_from="no-reply@careers.test")
    monkeypatch.setattr(config, "get", lambda: settings)
    auth._recent.clear()
    yield server
    server.shutdown()
    server.server_close()
