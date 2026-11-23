"""A script that records eval runs writes to the database DATABASE_URL_WORKER names, whose default is the live one, and
a finished run cannot be deleted. app.evals.scratch is the check that keeps such a script away from it: a scratch name,
or --live. These tests only read names; no database is connected to.
"""

import pytest
from sqlalchemy import text

from app.evals import scratch


def worker_at(monkeypatch, name):
    monkeypatch.setenv("DATABASE_URL_WORKER", f"postgresql://app_worker:placeholder@127.0.0.1:5433/{name}")


@pytest.mark.parametrize(
    "name",
    ["app_test", "app_p2_fix_run_test", "ci_local_evals_test", "evals"],  # evals: the CI eval job's database
)
def test_a_scratch_database_is_accepted(monkeypatch, name):
    worker_at(monkeypatch, name)
    assert scratch.refusal(live=False) is None


@pytest.mark.parametrize(
    "name", ["app", "postgres", "evals2", "my_evals", "app_test_backup", "test", "_test", "App_test", "migration_check"]
)
def test_any_other_database_is_refused_by_name_and_the_message_says_how_to_override(monkeypatch, name):
    worker_at(monkeypatch, name)
    message = scratch.refusal(live=False)
    assert message is not None and repr(name) in message and "--live" in message


@pytest.mark.parametrize("name", ["app", "postgres"])
def test_live_lets_a_run_go_to_any_database_that_was_named(monkeypatch, name):
    worker_at(monkeypatch, name)
    assert scratch.refusal(live=True) is None


@pytest.mark.parametrize("live", [False, True])
def test_a_database_nobody_named_is_refused_even_with_live_because_its_default_is_the_live_one(monkeypatch, live):
    monkeypatch.delenv("DATABASE_URL_WORKER", raising=False)
    assert "not set" in (scratch.refusal(live) or "")
    monkeypatch.setenv("DATABASE_URL_WORKER", "")  # an empty value is the same as none: the settings fall back too
    assert "not set" in (scratch.refusal(live) or "")


def test_only_the_workers_database_is_read_because_the_scripts_never_connect_as_the_migration_login(monkeypatch):
    worker_at(monkeypatch, "app_test")
    monkeypatch.setenv("DATABASE_URL_MIGRATE", "postgresql://app:placeholder@127.0.0.1:5433/app")
    assert scratch.refusal(live=False) is None


# ---- a replay refuses before it writes anything (05-FIXES B6) ----------------------------------------


@pytest.mark.parametrize(
    "variable", ["DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER", "DATABASE_URL_MATCH"]
)
@pytest.mark.parametrize(
    "suite", ["injection", "chat", "record_proposals", "tailoring_integrity", "evidence_faithfulness"]
)
def test_a_replay_with_any_of_the_four_logins_on_a_database_that_is_not_scratch_writes_nothing(
    engine, monkeypatch, variable, suite
):
    from sqlalchemy.engine import make_url

    from app.evals import runner
    from app.platform import config

    real = config.get()
    urls = {
        "DATABASE_URL_MIGRATE": real.database_url_migrate,
        "DATABASE_URL_API": real.database_url_api,
        "DATABASE_URL_WORKER": real.database_url_worker,
        "DATABASE_URL_MATCH": real.database_url_match,
    }
    urls[variable] = make_url(urls[variable]).set(database="app").render_as_string(hide_password=False)
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    with pytest.raises(runner.NotAScratchDatabase, match="'app'"):
        runner.run_suite(suite)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM eval_runs")).scalar_one() == 0


def test_a_database_called_evals_is_scratch_too(monkeypatch):
    from sqlalchemy.engine import make_url

    from app.evals import runner
    from app.platform import config

    real = config.get()
    names = {
        name: make_url(getattr(real, name.lower())).set(database="evals").render_as_string(hide_password=False)
        for name in ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER", "DATABASE_URL_MATCH")
    }
    monkeypatch.setattr(config, "get", lambda: config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **names}))
    runner.refuse_unless_scratch()  # does not raise
