"""Plan 08-01: the fake provider replays per principal (INDEX decision 12), so interleaved synthetic candidates each get
their scenario's answers from the first one. The gateway commits ledger rows on connections of its own, so these tests
use a database of their own, like the other gateway tests.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.platform import config, db
from app.platform.llm import complete, fake
from app.platform.llm.__main__ import PingResult
from tests.campaigns.factories import make_user

MESSAGES = [{"role": "user", "content": "say pong"}]
DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")


def recordings(tmp_path: Path, name: str, answers: list) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "ping.json").write_text(json.dumps([{"reply": answer} for answer in answers]), encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def clean_fake():
    yield
    for principal in list(fake._principals):
        fake.forget(principal)
    fake.replay_from(None)


def text_of(task: str, principal) -> str:
    return json.loads(fake.call(task, principal=principal).text)["reply"]


def test_interleaved_principals_each_get_the_answers_from_the_first(tmp_path):
    a, b = uuid4(), uuid4()
    directory = recordings(tmp_path, "scenario", ["zero", "one", "two"])
    fake.replay_for(a, directory)
    fake.replay_for(b, directory)
    order = [a, b, b, a, b, a]
    assert [text_of("ping", who) for who in order] == ["zero", "zero", "one", "one", "two", "two"]


def test_an_unregistered_principal_keeps_the_old_rules_and_forget_returns_a_principal_to_them(tmp_path):
    a = uuid4()
    scripted = recordings(tmp_path, "scripted", ["scripted", "scripted-again"])
    other = recordings(tmp_path, "other", ["global-a", "global-b"])
    fake.replay_from(other / "ping.json")
    fake.replay_for(a, scripted)
    assert text_of("ping", a) == "scripted"
    assert text_of("ping", uuid4()) == "global-a" and text_of("ping", None) == "global-b"
    fake.replay_from(other / "ping.json")  # starting the global list over does not clear a registration
    assert text_of("ping", a) == "scripted-again"  # its own counter went on: the registration was not cleared
    fake.forget(a)
    assert text_of("ping", a) == "global-a"


def test_registering_again_starts_that_principals_counters_over(tmp_path):
    a = uuid4()
    directory = recordings(tmp_path, "scenario", ["zero", "one"])
    fake.replay_for(a, directory)
    assert text_of("ping", a) == "zero"
    fake.replay_for(a, directory)
    assert text_of("ping", a) == "zero"


def test_a_registered_principal_with_no_file_for_the_task_raises_naming_the_task(tmp_path):
    a = uuid4()
    fake.replay_for(a, recordings(tmp_path, "scenario", ["zero"]))
    with pytest.raises(RuntimeError, match="writer.packet"):
        fake.call("writer.packet", principal=a)


def test_a_registered_principal_that_has_used_every_answer_raises(tmp_path):
    a = uuid4()
    fake.replay_for(a, recordings(tmp_path, "scenario", ["zero"]))
    fake.call("ping", principal=a)
    with pytest.raises(RuntimeError, match="no recorded answer left"):
        fake.call("ping", principal=a)


# ---- through the gateway -------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def principal_db(make_database, alembic_run) -> URL:
    url: URL = make_database(f"llm_principal_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def engine(principal_db: URL, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    eng = create_engine(principal_db.set(drivername="postgresql+psycopg"))
    with eng.begin() as c:
        c.execute(text("TRUNCATE llm_calls, model_registry, users CASCADE"))
    urls = {
        name: make_url(os.environ[name]).set(database=principal_db.database).render_as_string(hide_password=False)
        for name in DB_VARIABLES
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    yield eng
    for application_engine in db._engines.values():
        application_engine.dispose()
    eng.dispose()


def ledger(engine: Engine) -> list:
    with engine.connect() as c:
        return c.execute(text("SELECT user_id, outcome::text FROM llm_calls ORDER BY id")).all()


def test_the_gateway_passes_the_principal_through_and_the_cost_row_names_it(engine, tmp_path):
    with engine.begin() as c:
        a, b = make_user(c), make_user(c)
    directory = recordings(tmp_path, "scenario", ["zero", "one"])
    fake.replay_for(a, directory)
    fake.replay_for(b, directory)
    ask = lambda who: complete("ping", PingResult, MESSAGES, principal=who, prompt_version="v1")  # noqa: E731
    assert [ask(who).reply for who in (a, b, a, b)] == ["zero", "zero", "one", "one"]
    assert [row.user_id for row in ledger(engine)] == [a, b, a, b]


def test_a_task_the_principal_has_no_file_for_fails_the_call_and_leaves_no_ledger_row(engine, tmp_path):
    with engine.begin() as c:
        a = make_user(c)
    fake.replay_for(a, recordings(tmp_path, "scenario", ["zero"]))
    with pytest.raises(RuntimeError, match="chat.turn"):
        complete("chat.turn", PingResult, MESSAGES, principal=a, prompt_version="v1")
    assert ledger(engine) == []
