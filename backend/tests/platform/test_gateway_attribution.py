"""Plan 07-01: model calls made inside `attributed` are charged to a campaign and an agent run, and only those
(and the budget settings that cost is checked against).

The gateway commits its ledger rows on connections of its own, so these tests use a database of their own (named for
this process, migrated, dropped at the end of the session), like the other gateway tests.
"""

import json
import os
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.platform import config, db
from app.platform.llm import SchemaInvalid, attributed, complete, fake
from app.platform.llm.__main__ import PingResult
from tests.campaigns.factories import make_run, make_world

MESSAGES = [{"role": "user", "content": "say pong"}]
PONG = {"reply": "pong"}
DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")


@pytest.fixture(scope="module")
def attribution_db(make_database, alembic_run) -> URL:
    url: URL = make_database(f"llm_attribution_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def engine(attribution_db: URL, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(attribution_db.set(drivername="postgresql+psycopg"))
    with eng.begin() as c:
        c.execute(text("TRUNCATE llm_calls, model_registry, campaigns, agent_runs, jobs, job_sources, users CASCADE"))
    urls = {
        name: make_url(os.environ[name]).set(database=attribution_db.database).render_as_string(hide_password=False)
        for name in DB_VARIABLES
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    recorded = tmp_path / "recorded.json"
    recorded.write_text(json.dumps([PONG] * 8), encoding="utf-8")
    fake.replay_from(recorded)
    yield eng
    for application_engine in db._engines.values():
        application_engine.dispose()
    fake.replay_from(None)
    eng.dispose()


def ask(user=None):
    return complete("ping", PingResult, MESSAGES, principal=user, prompt_version="v1")


def ledger(engine: Engine) -> list:
    with engine.connect() as c:
        return c.execute(text("SELECT id, campaign_id, agent_run_id, user_id FROM llm_calls ORDER BY id")).all()


def world_and_run(engine: Engine):
    with engine.begin() as c:
        world = make_world(c)
        run = make_run(c, world.user, world.campaign, "researcher")
    return world, run


def test_a_call_inside_the_context_is_charged_to_the_campaign_and_the_run(engine):
    world, run = world_and_run(engine)
    with attributed(campaign_id=world.campaign, agent_run_id=run) as attribution:
        ask(world.user)
    (row,) = ledger(engine)
    assert (row.campaign_id, row.agent_run_id, row.user_id) == (world.campaign, run, world.user)
    assert attribution.call_ids == [row.id]


def test_a_call_made_from_a_nested_function_is_charged_too_and_every_id_is_collected(engine):
    world, run = world_and_run(engine)

    def deep():
        return [ask(world.user), ask(world.user)]

    def middle():
        return deep()

    with attributed(campaign_id=world.campaign, agent_run_id=run) as attribution:
        middle()
    rows = ledger(engine)
    assert [r.campaign_id for r in rows] == [world.campaign] * 2
    assert attribution.call_ids == [r.id for r in rows]


def test_a_call_outside_the_context_has_neither_column_and_the_context_ends_with_the_block(engine):
    world, run = world_and_run(engine)
    ask(world.user)
    with attributed(campaign_id=world.campaign, agent_run_id=run):
        pass
    ask(world.user)
    assert [(r.campaign_id, r.agent_run_id) for r in ledger(engine)] == [(None, None), (None, None)]


def test_blocks_nest_and_the_inner_one_wins_until_it_ends(engine):
    world, run = world_and_run(engine)
    with engine.begin() as c:
        other = make_run(c, world.user, world.campaign, "analyst")
    with attributed(campaign_id=world.campaign, agent_run_id=run) as outer:
        ask(world.user)
        with attributed(campaign_id=world.campaign, agent_run_id=other) as inner:
            ask(world.user)
        ask(world.user)
    assert [r.agent_run_id for r in ledger(engine)] == [run, other, run]
    assert len(outer.call_ids) == 2 and len(inner.call_ids) == 1


def test_the_context_does_not_leak_to_another_thread(engine):
    world, run = world_and_run(engine)
    seen: list = []

    def elsewhere() -> None:
        ask(world.user)
        seen.append(True)

    with attributed(campaign_id=world.campaign, agent_run_id=run) as attribution:
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join()
    assert seen == [True]
    (row,) = ledger(engine)
    assert (row.campaign_id, row.agent_run_id) == (None, None)
    assert attribution.call_ids == []


def test_every_attempt_of_a_call_that_fails_is_charged_as_well(engine, tmp_path):
    world, run = world_and_run(engine)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(["not json", "not json"]), encoding="utf-8")
    fake.replay_from(bad)
    with attributed(campaign_id=world.campaign, agent_run_id=run) as attribution:
        with pytest.raises(SchemaInvalid):
            ask(world.user)
    rows = ledger(engine)
    assert len(rows) == 2 and attribution.call_ids == [r.id for r in rows]
    assert {r.campaign_id for r in rows} == {world.campaign}


def test_the_api_ledger_charges_the_campaign_under_the_candidates_own_row(engine):
    world, run = world_and_run(engine)
    with attributed(campaign_id=world.campaign, agent_run_id=run) as attribution:
        complete("ping", PingResult, MESSAGES, principal=world.user, prompt_version="v1", ledger="api")
    (row,) = ledger(engine)
    assert row.campaign_id == world.campaign and attribution.call_ids == [row.id]


# ---- the budget and page-replay settings (07-01) ----


def test_the_budgets_default_to_seventy_five_cents_and_five_dollars_and_are_read_in_micro_dollars():
    settings = config.load({})
    assert (settings.campaign_budget_usd, settings.monthly_budget_usd) == (Decimal("0.75"), Decimal("5"))
    assert (settings.campaign_budget_microusd, settings.monthly_budget_microusd) == (750_000, 5_000_000)
    assert settings.research_pages_dir is None


def test_the_budgets_and_the_pages_directory_come_from_the_environment():
    settings = config.load(
        {"APP_CAMPAIGN_BUDGET_USD": "1.234567", "APP_MONTHLY_BUDGET_USD": "12", "RESEARCH_PAGES_DIR": "/srv/pages"}
    )
    assert settings.campaign_budget_microusd == 1_234_567 and settings.monthly_budget_microusd == 12_000_000
    assert settings.research_pages_dir == "/srv/pages"


@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "abc", "NaN", "Infinity", "0.0000001"])
@pytest.mark.parametrize("name", ["APP_CAMPAIGN_BUDGET_USD", "APP_MONTHLY_BUDGET_USD"])
def test_a_budget_that_is_not_a_positive_amount_is_a_config_error_that_does_not_echo_it(name, value):
    with pytest.raises(config.ConfigError) as refused:
        config.load({name: value})
    assert name in str(refused.value) and value not in str(refused.value).replace(name, "")


def test_recorded_pages_are_refused_when_the_app_is_in_production():
    with pytest.raises(config.ConfigError, match="RESEARCH_PAGES_DIR"):
        config.load({"APP_ENV": "production", "RESEARCH_PAGES_DIR": "/srv/pages"})
    assert config.load({"APP_ENV": "production"}).research_pages_dir is None
    assert (
        config.load({"APP_ENV": "development", "RESEARCH_PAGES_DIR": "/srv/pages"}).research_pages_dir == "/srv/pages"
    )


def test_the_example_environment_file_lists_the_new_settings_with_the_defaults():
    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text(encoding="utf-8")
    assert "APP_CAMPAIGN_BUDGET_USD=0.75" in example and "APP_MONTHLY_BUDGET_USD=5" in example
    assert "RESEARCH_PAGES_DIR" in example
