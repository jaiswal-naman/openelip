"""The world the campaign graph tests run in (plan 07-04): a candidate with a confirmed record and the AI consent, an
open
job at the invented Lumen Freight, a shown match decision with two verdicts, a ready tailored resume and a campaign,
plus
the fake provider and page directory the graph uses, and small helpers to run the queue and to record what the
candidate and the employer do. Everything is invented (Ada Example, Lumen Freight, addresses at .example)."""

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.campaigns import jobs as campaign_jobs  # noqa: F401  (registers the campaign handlers)
from app.guardian import jobs as guardian_jobs  # noqa: F401  (registers guardian.release)
from app.platform import config, db, queue, relay
from app.platform.llm import fake
from app.profile import api as profile
from app.trust import api as trust
from tests.campaigns.factories import make_campaign
from tests.match.factories import Verdict, make_run, make_shown_decision
from tests.p5_factories import make_job, make_tailorable_record, make_tailored_resume, make_user

HERE = Path(__file__).parent
PAGES = HERE / "pages"
RECORDED = HERE / "recorded"
POSTING = "Build reporting pipelines in Python. Experience with PostgreSQL."
REQUIREMENTS = (("Build reporting pipelines in Python", "required"), ("Experience with PostgreSQL", "required"))
APPLY_URL = "https://careers.lumen.example/apply/lumen-1"
TYPED_ADDRESS = "talent@lumen.example"
APPLIED_ON = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
LEASE = "300 seconds"


@dataclass(frozen=True)
class World:
    user: UUID
    job_id: int
    campaign: UUID
    facts: dict[str, UUID]


def use_fakes(monkeypatch: pytest.MonkeyPatch, *, pages: Path | None = PAGES, recorded: Path | None = RECORDED) -> None:
    """The gateway on the fake provider with the graph's recordings, pages from the replay directory, and the apply link
    check answering `alive` (no network)."""
    settings = replace(
        config.get(), llm_provider="fake", research_pages_dir=None if pages is None else str(pages), app_env="test"
    )
    monkeypatch.setattr(config, "get", lambda: settings)
    if recorded is not None:
        monkeypatch.setenv("LLM_FAKE_DIR", str(recorded))
    fake.replay_from(None)
    from app.ingest import api as ingest

    monkeypatch.setattr(ingest, "verify_live", lambda conn, job_id: True)


def build_world(
    engine: Engine,
    *,
    follow_up_email: str | None = None,
    budget_microusd: int = 750_000,
    stage: str = "researching",
    description: str = POSTING,
    with_tailored: bool = True,
    with_decision: bool = True,
    follow_up_after_days: int = 7,
) -> World:
    with engine.begin() as c:
        user = make_user(c)
        record = make_tailorable_record(c, user)
        trust.grant_consent(c, user, "ai_processing", trust.ai_processing_text_version())
        job = make_job(
            c, title="Data engineer", description=description, requirements=REQUIREMENTS, company_name="Lumen Freight"
        )
        c.execute(text("UPDATE jobs SET apply_url = :u WHERE id = :j"), {"u": APPLY_URL, "j": job.id})
        run = make_run(c, user)
        if with_decision:
            make_shown_decision(
                c,
                run,
                user,
                job,
                [Verdict("met", (record.facts["bullet_initech_1"],)), Verdict("no_evidence")],
            )
        if with_tailored:
            make_tailored_resume(c, user, job)
        campaign = make_campaign(
            c,
            user,
            job.id,
            stage=stage,
            follow_up_email=follow_up_email,
            budget_microusd=budget_microusd,
            follow_up_after_days=follow_up_after_days,
        )
        assert profile.usable_facts(c, user)
    return World(user, job.id, campaign, dict(record.facts))


def run_jobs(limit: int = 300) -> int:
    """Run every due queue job (and the relay after each) until nothing is due; returns how many ran."""
    ran = 0
    while ran < limit:
        done = queue.run_once(lambda: db.transaction(role="worker"), "test-worker")
        with db.transaction(role="worker") as conn:
            relay.relay_once(conn)
        if not done:
            return ran
        ran += done
    raise AssertionError("the queue never went idle")


def resume(world: World, cause: str = "start") -> None:
    """Enqueue a resume of the campaign's thread, the way the start path and the event subscribers do."""
    from app.campaigns import stages

    with db.transaction(world.user, role="worker") as conn:
        stages.enqueue_wake(conn, world.campaign, world.user, cause)


def record_application(engine: Engine, world: World, status: str, *, when: datetime = APPLIED_ON) -> UUID:
    """The candidate records a status of the campaign's application (the first one creates and links it)."""
    with engine.begin() as c:
        found = c.execute(
            text("SELECT id FROM applications WHERE campaign_id = :c"), {"c": world.campaign}
        ).scalar_one_or_none()
        if found is None:
            found = c.execute(
                text(
                    """INSERT INTO applications (user_id, job_id, status, status_changed_at, campaign_id)
                       VALUES (:u, :j, CAST(:s AS application_status), :at, :c) RETURNING id"""
                ),
                {"u": world.user, "j": world.job_id, "s": status, "at": when, "c": world.campaign},
            ).scalar_one()
        else:
            c.execute(
                text(
                    "UPDATE applications SET status = CAST(:s AS application_status), "
                    "status_changed_at = :at WHERE id = :a"
                ),
                {"s": status, "at": when, "a": found},
            )
        c.execute(
            text(
                """INSERT INTO application_events (application_id, user_id, status, occurred_at)
                   VALUES (:a, :u, CAST(:s AS application_status), :at)"""
            ),
            {"a": found, "u": world.user, "s": status, "at": when},
        )
        c.execute(text("UPDATE campaigns SET last_update_at = now() WHERE id = :c"), {"c": world.campaign})
    return UUID(str(found))


def approve(world: World, approval_id: UUID, payload: dict[str, Any]) -> str:
    """The candidate approves one proposed action on the API login; the worker's release then runs."""
    from app.guardian import api as guardian

    with db.transaction(world.user) as conn:
        guardian.decide(conn, world.user, approval_id, approve=True, payload_hash=guardian.payload_hash(payload))
    return str(guardian.release(approval_id, None))


def one(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), params))


def stage(engine: Engine, world: World) -> str:
    return str(one(engine, "SELECT stage::text FROM campaigns WHERE id = :c", c=world.campaign))


def steps(engine: Engine, world: World) -> dict[str, str]:
    return {
        r.step_key: r.status
        for r in rows(
            engine,
            "SELECT step_key, status::text AS status FROM campaign_steps WHERE campaign_id = :c",
            c=world.campaign,
        )
    }


def age(engine: Engine, world: World, days: int = 8) -> None:
    """Move the clock `days` on: last news, every step's due time and every wake job's run time that many days back."""
    with engine.begin() as c:
        c.execute(
            text(f"UPDATE campaigns SET last_update_at = last_update_at - interval '{int(days)} days' WHERE id = :c"),
            {"c": world.campaign},
        )
        c.execute(
            text(f"UPDATE campaign_steps SET due_at = due_at - interval '{int(days)} days' WHERE campaign_id = :c"),
            {"c": world.campaign},
        )
        c.execute(
            text(
                f"""UPDATE queue_jobs SET run_at = run_at - interval '{int(days)} days'
                    WHERE kind = 'campaign.wake' AND status = 'queued'"""
            )
        )


def env_for_subprocess(recorded: Path = RECORDED, pages: Path = PAGES) -> dict[str, str]:
    """The environment a crash-driver subprocess runs with: the test database, the fake provider and the fixtures."""
    return {**os.environ, "LLM_PROVIDER": "fake", "LLM_FAKE_DIR": str(recorded), "RESEARCH_PAGES_DIR": str(pages)}


Check = Callable[[], None]
