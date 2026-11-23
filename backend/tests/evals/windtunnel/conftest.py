"""Wind-tunnel tests run on the shared test database with the application's own settings (not the private database of
tests/evals/conftest.py): the tunnel needs the real cached settings, and it commits through logins of its own.

Every test module starts on emptied tables (INDEX decision 37: the tunnel needs a quiet database), and the fake
provider's registrations are cleared around each test.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.platform.llm import fake
from tests.conftest import DATA_TABLES


@pytest.fixture
def engine(database_url: URL) -> Iterator[Engine]:
    """A bootstrap engine on the shared test database (replaces the private one of tests/evals/conftest.py)."""
    eng = create_engine(database_url.set(drivername="postgresql+psycopg"))
    yield eng
    eng.dispose()


def empty(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True, scope="module")
def _quiet_database(database_url: URL) -> Iterator[None]:
    eng = create_engine(database_url.set(drivername="postgresql+psycopg"))
    empty(eng)
    yield
    empty(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def _fake_is_clean() -> Iterator[None]:
    fake.replay_from(None)
    yield
    fake.replay_from(None)
