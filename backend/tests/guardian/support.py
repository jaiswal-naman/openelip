"""Shared pieces of the plan 07-03 tests: a committed world (a person with consent, a job and a campaign), the
payload of a
follow-up and its provenance, and the provider that answers verify.entail. Everything is invented (Ada Example,
Lumen Demo,
addresses at .example); no model is called.

The real-role tests commit their rows (db.transaction opens its own connections) and use the `clean` fixture."""

import json
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.platform import db
from app.trust import api as trust
from tests.campaigns.factories import make_campaign, make_run
from tests.match.factories import make_job, make_user
from tests.tailor.fixtures.service.harness import use_provider, write_answers

TYPED_ADDRESS = "talent@lumen.example"
APPLIED = "I applied on 3 November 2026."
SENTENCE = {"field": "body", "text": APPLIED, "events": [APPLIED]}
CODE_PARTS = ["Hello,", "Best regards,\nAda"]


class World(NamedTuple):
    user: UUID
    job_id: int
    campaign: UUID


def make_committed_world(engine: Engine, *, follow_up_email: str | None = TYPED_ADDRESS, consent: bool = True) -> World:
    with engine.begin() as conn:
        user = make_user(conn)
        job = make_job(
            conn,
            title="Data engineer",
            description="Build Python pipelines.",
            requirements=[("Python pipelines", "required")],
            company_name="Lumen Demo",
        )
        campaign = make_campaign(conn, user, job.id, follow_up_email=follow_up_email)
        if consent:
            trust.grant_consent(conn, user, "ai_processing", trust.ai_processing_text_version())
    return World(user, job.id, campaign)


def follow_up_payload(body_sentence: str = APPLIED, *, to: str = TYPED_ADDRESS) -> dict[str, Any]:
    return {
        "channel": "email",
        "to": to,
        "reply_to": "ada@example.test",
        "subject": "Following up on my Data engineer application",
        "body": f"Hello,\n\n{body_sentence}\n\nBest regards,\nAda",
    }


def follow_up_provenance(**extra: Any) -> dict[str, Any]:
    return {
        "fields": {"subject": "code", "body": "model"},
        "sentences": [dict(SENTENCE)],
        "code_parts": CODE_PARTS,
        **extra,
    }


def packet_payload(job_id: int) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "apply_url": "https://careers.lumen.example/apply",
        "packet_id": None,
        "tailored_resume_id": None,
        "answers": [{"question_id": "why", "question": "Why us?", "status": "candidate_only", "answer": ""}],
        "cover_note": "",
    }


def entails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, times: int = 12, ok: bool = True, smtp: Any = None
) -> list[tuple[Any, ...]]:
    """The gateway on the fake provider, answering `verify.entail` for one sentence `times` times; returns the call log.
    With `smtp` (the fake mail server) the relay also sends to it."""
    answers = [{"items": [{"edit": "e1", "entailed": ok}]} for _ in range(times)]
    calls = use_provider(monkeypatch, write_answers(tmp_path, *answers))
    if smtp is not None:
        from dataclasses import replace

        from app.platform import config

        settings = replace(config.get(), smtp_url=smtp.url(), mail_from="no-reply@careers.test")
        monkeypatch.setattr(config, "get", lambda: settings)
    return calls


def run_as_worker(world: World, role: str = "chaser") -> UUID:
    with db.transaction(world.user, role="worker") as conn:
        return make_run_committed(conn, world, role)


def make_run_committed(conn: Any, world: World, role: str) -> UUID:
    return make_run(conn, world.user, world.campaign, role)


def propose_follow_up(
    world: World,
    *,
    step: str = "follow_up:1",
    payload: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
):
    """Propose a follow-up for the world's campaign as the worker, in one transaction."""
    from app.guardian import api as guardian

    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "chaser", "test-1")
        return guardian.propose_action(
            conn,
            run,
            action_type="send_follow_up",
            payload=payload or follow_up_payload(),
            provenance=provenance or follow_up_provenance(),
            inputs={"step_id": step},
        )


def status_of(engine: Engine, action_id: int) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT status::text FROM agent_actions WHERE id = :i"), {"i": action_id}).scalar_one()


def dumps(value: Any) -> str:
    return json.dumps(value)


def add_spend(engine: Engine, world: World, micro: int, *, campaign: bool = True, task: str = "chaser.draft") -> int:
    """One invented ledger row of `micro` micro-dollars for the world's person (and campaign unless told otherwise)."""
    with engine.begin() as conn:
        model = conn.execute(
            text(
                """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
                   VALUES ('local', 'test-model-' || gen_random_uuid(), 'chat', true, 'test') RETURNING id"""
            )
        ).scalar_one()
        return int(
            conn.execute(
                text(
                    """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id,
                                              cost_microusd, latency_ms)
                       VALUES (:t, 'v1', :m, 'ok', :u, :c, :cost, 1) RETURNING id"""
                ),
                {"t": task, "m": model, "u": world.user, "c": world.campaign if campaign else None, "cost": micro},
            ).scalar_one()
        )


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), params))


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def approve_and_release(world: World, proposed: Any, payload: dict[str, Any] | None = None) -> str:
    """The candidate approves what was proposed (on the API login), then the worker's release runs."""
    from app.guardian import api as guardian

    with db.transaction(world.user) as conn:
        guardian.decide(
            conn,
            world.user,
            proposed.approval_id,
            approve=True,
            payload_hash=guardian.payload_hash(payload or follow_up_payload()),
        )
    return str(guardian.release(proposed.approval_id, None))


def relay_now() -> int:
    from app.platform import relay

    with db.transaction(role="worker") as conn:
        return relay.relay_once(conn)


def make_due(engine: Engine, action_id: int, *, window_over: bool = True) -> None:
    """The undo window is over: the message is due and (as it would be) the action's undo_until is in the past."""
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        if window_over:
            c.execute(
                text("UPDATE agent_actions SET undo_until = now() - interval '2 minutes' WHERE id = :i"),
                {"i": action_id},
            )
        c.execute(
            text(
                "UPDATE outbox SET not_before = now() - interval '1 minute' "
                "WHERE agent_action_id = :i AND not_before <> 'infinity'"
            ),
            {"i": action_id},
        )
