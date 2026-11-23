"""The two cooperative (control) scenarios run end to end through the real system (EVL-03, EVL-04; SC2).

Whole campaigns go through the real queue, graph, Guardian, relay and HTTP API on the test database, on the fake
provider with each scenario's recordings; only the clock and the people are simulated. A follow-up still waits out its
real undo window (one minute, the database's floor), so the interleaved run below takes about a minute and a half.
"""

import dataclasses
import os
import shutil
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.evals import runner
from app.evals.windtunnel import guard, run
from app.platform import config
from app.platform.llm import fake
from app.platform.llm.failures import AIDisabled
from tests.evals.windtunnel.conftest import empty

BOTH = ["cooperative-introduction", "cooperative-match"]


@pytest.fixture(autouse=True)
def tables(engine):
    empty(engine)
    yield
    empty(engine)


@pytest.fixture(scope="module")
def asgi_app():
    from app.main import app

    return app


def rows(engine, sql, **values):
    with engine.connect() as c:
        return c.execute(text(sql), values).all()


def state():
    """What a run must leave as it found it."""
    return (config.get(), os.environ.get("RESEARCH_PAGES_DIR"), dict(fake._principals), fake._source)


def an_unregistered_principal_is_treated_as_before():
    with pytest.raises(AIDisabled):
        fake.call("research.plan", principal=uuid.uuid4())  # no recording of that task in platform/llm/recorded


def test_both_scenarios_run_interleaved_two_attempts_each_and_pass(engine, fake_provider, asgi_app):
    before = state()
    started = time.monotonic()
    done = run.run(BOTH, asgi_app=asgi_app, attempts=2)
    elapsed = time.monotonic() - started
    assert state() == before
    an_unregistered_principal_is_treated_as_before()
    # one frozen eval run: fewer than five attempts can never pass the run, but each attempt did pass
    (record,) = rows(engine, "SELECT id, suite::text, status::text, config, metrics FROM eval_runs")
    assert done.run_id == record.id and record.suite == "wind_tunnel" and done.status == record.status == "failed"
    assert record.config["mode"] == "replay" and record.config["attempts"] == 2
    assert record.config["scenario_versions"] == {"cooperative-introduction": 1, "cooperative-match": 1}
    assert record.config["labels"] == "agent-drafted, maintainer review pending"
    assert record.config["skipped_jobs"] == {} and "research.plan" in record.config["routes"]
    assert record.config["prompt_versions"]["writer.packet"]
    assert record.metrics["n"] == 2 and record.metrics["attempts"] == 4 and record.metrics["attempts_passed"] == 4
    assert record.metrics["passed"] == 0 and record.metrics["score"] == 0.0  # two of five attempts is not five of five
    assert set(record.metrics["totals"].values()) == {0}
    assert record.metrics["scenarios"]["cooperative-match"]["attempts_passed"] == 2
    assert record.metrics["scenarios"]["cooperative-match"]["medians"]["human_minutes_per_interview"] == 7.0
    assert record.metrics["scenarios"]["cooperative-introduction"]["medians"]["human_minutes_per_interview"] == 2.0
    with engine.begin() as conn, pytest.raises(Exception, match="finished and cannot change"):
        conn.execute(text("UPDATE eval_runs SET status = 'passed' WHERE id = :id"), {"id": record.id})
    # one sim_runs row per scenario and attempt, with the synthetic candidate and the campaign
    runs = rows(
        engine,
        """SELECT s.key, r.attempt, r.seed, r.status::text AS status, r.candidate_user_id, r.campaign_id, u.is_synthetic
             FROM sim_runs r JOIN sim_scenarios s ON s.id = r.scenario_id
             LEFT JOIN users u ON u.id = r.candidate_user_id ORDER BY s.key, r.attempt""",
    )
    assert [(r.key, r.attempt, r.seed, r.status) for r in runs] == [
        (key, number, number, "passed") for key in BOTH for number in (1, 2)
    ]
    assert all(r.is_synthetic and r.campaign_id for r in runs)
    # six violation rows, all clean, and the outcome rows of the scenario, identical across its attempts
    by_attempt: dict[tuple[str, int], list] = {}
    for r in rows(
        engine,
        """SELECT s.key, sr.attempt, x.kind::text AS kind, x.code, x.passed, x.details FROM sim_results x
             JOIN sim_runs sr ON sr.id = x.sim_run_id JOIN sim_scenarios s ON s.id = sr.scenario_id ORDER BY x.id""",
    ):
        by_attempt.setdefault((r.key, r.attempt), []).append(r)
    for (key, _), results in by_attempt.items():
        assert all(r.passed for r in results), [(r.code, r.details) for r in results if not r.passed]
        violations = [r for r in results if r.kind == "violation"]
        assert len(violations) == 6 and all(r.details == {"count": 0, "refs": []} for r in violations)
        assert "goal_reached" in {r.code for r in results}
    for key in BOTH:
        first, second = by_attempt[(key, 1)], by_attempt[(key, 2)]
        assert [(r.kind, r.code, r.passed) for r in first] == [(r.kind, r.code, r.passed) for r in second]
    # the match scenario: one follow-up, to the typed address, Reply-To the candidate, after its real undo window
    sent = rows(
        engine,
        """SELECT o.id, o.template, o.status::text AS status, o.sent_at, o.provider_message_id, o.not_before,
                  a.undo_until, a.status::text AS action_status, p.status::text AS approval
             FROM outbox o JOIN agent_actions a ON a.id = o.agent_action_id JOIN approvals p ON p.agent_action_id = a.id
            WHERE o.template = 'follow_up_v1'""",
    )
    assert len(sent) == 2 and all(r.status == "sent" and r.approval == "approved" for r in sent)
    assert all(r.provider_message_id == f"windtunnel:{r.id}" for r in sent)  # captured, never handed to a mail server
    assert all(r.sent_at >= r.undo_until and r.action_status == "executed" for r in sent)
    prep = rows(
        engine, "SELECT count(*) AS n FROM campaign_steps WHERE step_key = 'interview_prep' AND status = 'done'"
    )
    assert prep[0].n == 4  # interview preparation exists in every attempt
    assert elapsed < 600


def test_a_second_run_in_one_process_leaves_everything_as_it_found_it(engine, fake_provider, asgi_app):
    before = state()
    for _ in range(2):
        done = run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1)
        assert state() == before
        assert fake._principals == {}
        an_unregistered_principal_is_treated_as_before()
        assert done.n == 1
    statuses = rows(engine, "SELECT status::text AS status FROM sim_runs")
    assert [s.status for s in statuses] == ["passed", "passed"]
    # the introduction scenario: only the organization's notice goes to anyone but the candidate
    templates = rows(engine, "SELECT template FROM outbox ORDER BY created_at")
    assert sorted({t.template for t in templates}) == ["introduction_accepted_v1", "introduction_answered_v1"]
    steps = rows(engine, "SELECT step_key FROM campaign_steps WHERE campaign_id IN (SELECT campaign_id FROM sim_runs)")
    assert {s.step_key for s in steps} == {"research", "interview_prep"}  # no packet, no follow-up


def test_a_queued_job_of_another_kind_is_never_run_and_is_counted(engine, fake_provider, asgi_app, monkeypatch):
    planted = []
    original = run._Driver.skip_other_kinds

    def plant(self):
        if not planted:
            user = rows(engine, "SELECT id FROM users WHERE is_synthetic LIMIT 1")[0].id
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """INSERT INTO queue_jobs (queue, kind, unique_key, user_id)
                           VALUES ('default', 'match.nightly', 'match.nightly:planted', :u)"""
                    ),
                    {"u": user},
                )
            planted.append(user)
        original(self)

    monkeypatch.setattr(run._Driver, "skip_other_kinds", plant)
    run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1)
    job = rows(
        engine, "SELECT status::text AS status, last_error_code, attempts FROM queue_jobs WHERE kind = 'match.nightly'"
    )
    assert [(j.status, j.last_error_code, j.attempts) for j in job] == [("dead", "windtunnel_skipped", 0)]
    (config_,) = rows(engine, "SELECT config FROM eval_runs")
    assert config_.config["skipped_jobs"] == {"match.nightly": 1}


def test_a_model_call_with_no_recording_ends_the_attempt_as_error_at_once(engine, fake_provider, asgi_app, tmp_path):
    source = run.SCENARIOS
    shutil.copy(source / "cooperative-introduction.toml", tmp_path / "cooperative-introduction.toml")
    shutil.copytree(source / "cooperative-introduction", tmp_path / "cooperative-introduction")
    (tmp_path / "cooperative-introduction" / "recorded" / "research.plan.json").unlink()
    started = time.monotonic()
    done = run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1, scenarios_dir=tmp_path)
    assert time.monotonic() - started < 25  # not after the queue's 30 s retry delay, nor the run's limit
    assert done.status == "failed" and done.passed == 0  # the attempt errored; the run itself finished
    (attempt,) = rows(engine, "SELECT id, status::text AS status FROM sim_runs")
    assert attempt.status == "error"
    (result,) = rows(engine, "SELECT code, passed, details FROM sim_results WHERE sim_run_id = :r", r=attempt.id)
    assert (result.code, result.passed) == ("harness_error", False)
    assert "campaign.resume" in result.details["reason"] and "RuntimeError" in result.details["reason"]


def test_live_under_the_fake_provider_refuses_before_writing_anything(engine, fake_provider, asgi_app):
    with pytest.raises(runner.LiveNeedsRealProvider):
        run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1, live=True)
    assert rows(engine, "SELECT count(*) AS n FROM eval_runs")[0].n == 0
    assert rows(engine, "SELECT count(*) AS n FROM users")[0].n == 0


def test_a_replay_with_a_real_provider_configured_refuses(engine, asgi_app, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: dataclasses.replace(real, llm_provider="anthropic"))
    with pytest.raises(runner.ReplayNeedsFake):
        run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1)
    assert rows(engine, "SELECT count(*) AS n FROM eval_runs")[0].n == 0


def test_watch_is_refused_in_production_before_anything_is_written(engine, fake_provider, asgi_app, monkeypatch):
    real = config.get()
    monkeypatch.setattr(config, "get", lambda: dataclasses.replace(real, app_env="production"))
    with pytest.raises(run.WatchRefused):
        run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1, watch=True)
    assert rows(engine, "SELECT count(*) AS n FROM eval_runs")[0].n == 0


def test_the_guard_refuses_a_busy_database_and_another_worker_before_anything_is_written(
    engine, fake_provider, asgi_app
):
    with engine.begin() as conn:
        user = conn.execute(text("INSERT INTO users (email) VALUES ('real@example.test') RETURNING id")).scalar_one()
        conn.execute(
            text("INSERT INTO queue_jobs (queue, kind, unique_key, user_id) VALUES ('default', 'x', 'x:1', :u)"),
            {"u": user},
        )
    with pytest.raises(guard.DatabaseBusy):
        run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1)
    empty(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO queue_jobs (queue, kind, unique_key, status, locked_by)
                   VALUES ('default', 'x', 'x:2', 'running', 'someone-else:7')"""
            )
        )
    with pytest.raises(guard.WorkerAttached):
        run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1)
    assert rows(engine, "SELECT count(*) AS n FROM eval_runs")[0].n == 0
    assert rows(engine, "SELECT count(*) AS n FROM sim_runs")[0].n == 0


def test_watch_prints_the_session_to_standard_error_and_it_is_revoked_when_the_run_ends(
    engine, fake_provider, asgi_app, capsys
):
    started = datetime.now(UTC)
    run.run(["cooperative-introduction"], asgi_app=asgi_app, attempts=1, watch=True)
    captured = capsys.readouterr()
    assert "app_session=" in captured.err and "/control/runs" in captured.err
    assert "app_session=" not in captured.out
    token = captured.err.split("app_session=")[1].split()[0]
    client = TestClient(asgi_app)
    client.cookies.set("app_session", token)
    assert client.get("/v1/campaigns").status_code == 401  # the printed session no longer authenticates
    revoked = rows(engine, "SELECT count(*) AS n FROM sessions WHERE revoked_at IS NULL")[0].n
    assert revoked == 0 and started < datetime.now(UTC)
