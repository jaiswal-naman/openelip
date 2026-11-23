"""The world the Twin graph tests run in (plan 09-03): an organization with a member, an open role and a candidate who
accepted it (tests/twin/factories.py), the candidate's confirmed record (three resume facts, `f1` to `f3` to the model,
and any invented ones a test adds), the fake provider with a recorded answer directory per case, and small helpers that
do what the employer, the candidate and the worker do. Everything is invented (Halden Demo, Demo Candidate,
example.test).

Recordings are wiring checks, not model quality: `recorded/<case>/twin.classify.json` and `twin.answer.json` list the
answers the fake gives in order, so each case holds more answers than its test asks for."""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.campaigns import jobs as campaign_jobs  # noqa: F401  (registers graph.delete)
from app.guardian import api as guardian
from app.guardian import jobs as guardian_jobs  # noqa: F401  (registers guardian.release)
from app.platform import config, db, queue
from app.platform.llm import fake
from app.twin import api as twin
from app.twin import jobs as twin_jobs  # noqa: F401  (registers twin.resume)
from tests.twin.factories import TwinWorld, twin_world

HERE = Path(__file__).parent
RECORDED = HERE / "recorded"
OWN_EMAIL = "demo.candidate@example.test"  # an invented address a test may write into a fact (must be refused)
DEFAULT_FACTS = {
    "f1": "Senior data engineer at Initech Demo, 2019 to 2024.",
    "f2": "Built nightly Python and SQL pipelines that cut report time by 40 percent.",
    "f3": "Led a team of three engineers.",
}


@dataclass(frozen=True)
class GWorld:
    world: TwinWorld
    session: UUID
    extra: dict[str, UUID]  # facts a test added, by the name it gave

    @property
    def candidate(self) -> UUID:
        return self.world.candidate.id

    @property
    def member(self) -> UUID:
        assert self.world.member.user is not None
        return self.world.member.user


def use_fakes(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    """The gateway on the fake provider with this case's recordings."""
    settings = replace(config.get(), llm_provider="fake", app_env="test")
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setenv("LLM_FAKE_DIR", str(RECORDED / case))
    fake.replay_from(None)


def build(
    engine: Engine,
    *,
    dial: str | None = None,
    topics: dict[str, str] | None = None,
    facts: tuple[tuple[str, str, str], ...] = (),
) -> GWorld:
    """The world. `dial` sets the `twin_answer` level (none: the default `ask_first`), `topics` the permissions, `facts`
    are (name, kind, statement) of confirmed facts added after the three resume ones (so `f4`, `f5`, ...)."""
    with engine.begin() as c:
        world = twin_world(c)
        session = c.execute(
            text(
                """INSERT INTO twin_sessions (introduction_id, user_id, organization_id, opened_by)
                   VALUES (:i, :u, :o, :b) RETURNING id"""
            ),
            {"i": world.introduction, "u": world.candidate.id, "o": world.org.id, "b": world.member.user},
        ).scalar_one()
        if dial is not None:
            c.execute(
                text(
                    """INSERT INTO autonomy_settings (user_id, action_type, level)
                       VALUES (:u, 'twin_answer', CAST(:l AS autonomy_level))"""
                ),
                {"u": world.candidate.id, "l": dial},
            )
        for topic, permission in (topics or {}).items():
            c.execute(
                text(
                    """INSERT INTO twin_scopes (user_id, topic, permission)
                       VALUES (:u, CAST(:t AS twin_topic), CAST(:p AS twin_permission))"""
                ),
                {"u": world.candidate.id, "t": topic, "p": permission},
            )
        extra = {}
        for number, (name, kind, statement) in enumerate(facts, 1):
            extra[name] = _fact(c, world.candidate.id, kind, statement, number)
    return GWorld(world, session, extra)


def _fact(c: Any, user: UUID, kind: str, statement: str, number: int) -> UUID:
    event = c.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', 'fact', 'create', CAST(:after AS jsonb), 'candidate_edit', 1) RETURNING id"""
        ),
        {"u": user, "after": json.dumps({"kind": kind})},
    ).scalar_one()
    return c.execute(  # type: ignore[no-any-return]
        text(
            """INSERT INTO profile_facts (user_id, kind, statement, source, profile_event_id, created_at, confirmed_at)
               VALUES (:u, CAST(:k AS fact_kind), :s, 'candidate_edit', :e, now() + :n * interval '1 second', now())
               RETURNING id"""
        ),
        {"u": user, "k": kind, "s": statement, "e": event, "n": number},
    ).scalar_one()


def ask(w: GWorld, question: str = "What did you build at your last job?") -> UUID:
    """An employer member asks (the real intake, on the API login with row-level security) and the job is queued."""
    with db.transaction(w.member) as conn:
        asked = twin.ask(
            conn, introduction_id=w.world.introduction, question=question, asked_by=w.member, agent_token_id=None
        )
    return asked.question_id


def run_jobs(limit: int = 200) -> int:
    """Run every due queue job until nothing is due; returns how many ran."""
    ran = 0
    while ran < limit:
        done = queue.run_once(lambda: db.transaction(role="worker"), "test-worker")
        if not done:
            return ran
        ran += done
    raise AssertionError("the queue never went idle")


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), params))


def one(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def twin_messages(engine: Engine, question: UUID) -> list[tuple[str, str | None, str | None]]:
    """What the Twin said about the question, oldest first: (author, answer kind, topic)."""
    return [
        (r.author, r.answer_kind, r.topic)
        for r in rows(
            engine,
            """SELECT author::text AS author, answer_kind::text AS answer_kind, topic::text AS topic FROM twin_messages
                WHERE in_reply_to = :q ORDER BY created_at, id""",
            q=question,
        )
    ]


def finals(engine: Engine, question: UUID) -> list[Any]:
    return rows(
        engine,
        """SELECT answer_kind::text AS kind, body, topic::text AS topic, cited_fact_ids, agent_action_id
             FROM twin_messages WHERE in_reply_to = :q AND author = 'twin'
              AND answer_kind IN ('answered', 'relayed', 'declined')""",
        q=question,
    )


def pending(engine: Engine, question: UUID) -> Any:
    """The question's approval waiting for the candidate: (approval id, action id, payload)."""
    found = rows(
        engine,
        """SELECT p.id AS approval_id, a.id AS action_id, a.payload FROM approvals p
           JOIN agent_actions a ON a.id = p.agent_action_id
           WHERE p.status = 'pending' AND a.action_type = 'twin_answer' AND a.payload ->> 'question_id' = :q""",
        q=str(question),
    )
    assert len(found) == 1, found
    return found[0]


def approve(
    w: GWorld, approval_id: UUID, payload: dict[str, Any], *, reply: str | None = None, save: bool = False
) -> Any:
    """What the approve route does for the candidate: the Twin's hook first, then the decision with its edits."""
    with db.transaction(w.candidate) as conn:
        view = guardian.approval_view(conn, w.candidate, approval_id)
        hook = guardian.approve_hook("twin_answer")
        assert hook is not None and view is not None
        result = hook(conn, w.candidate, view, reply=reply, save_to_record=save)
        guardian.decide(
            conn,
            w.candidate,
            approval_id,
            approve=True,
            payload_hash=guardian.payload_hash(payload),
            edits=result.edits,
        )
    return result


def reject(w: GWorld, approval_id: UUID) -> None:
    with db.transaction(w.candidate) as conn:
        view = guardian.approval_view(conn, w.candidate, approval_id)
        assert view is not None
        guardian.decide(conn, w.candidate, approval_id, approve=False, payload_hash=view["payload_hash"])


def checkpoints(engine: Engine, question: UUID) -> int:
    return int(one(engine, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t", t=f"twin:{question}"))


def queued_resumes(engine: Engine) -> int:
    return int(one(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'twin.resume' AND status = 'queued'"))


def settled(engine: Engine, question: UUID) -> None:
    """The guard every path ends with: the queue is drained and the question has exactly one final Twin message, no
    queued resume, no checkpoint rows, no action still open."""
    run_jobs()
    assert len(finals(engine, question)) == 1, twin_messages(engine, question)
    assert queued_resumes(engine) == 0
    assert checkpoints(engine, question) == 0
    open_actions = one(
        engine,
        """SELECT count(*) FROM agent_actions WHERE action_type = 'twin_answer'
              AND status IN ('proposed', 'awaiting_approval', 'scheduled')""",
    )
    assert open_actions == 0


def add_spend(engine: Engine, w: GWorld, micro: int) -> None:
    """One invented ledger row of `micro` micro-dollars charged to a Twin run of the world's session."""
    with engine.begin() as c:
        run = c.execute(
            text(
                """INSERT INTO agent_runs (user_id, agent_role, config_version, twin_session_id)
                   VALUES (:u, 'twin', 'test', :s) RETURNING id"""
            ),
            {"u": w.candidate, "s": w.session},
        ).scalar_one()
        model = c.execute(
            text(
                """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
                   VALUES ('local', 'test-model-' || gen_random_uuid(), 'chat', true, 'test') RETURNING id"""
            )
        ).scalar_one()
        c.execute(
            text(
                """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, agent_run_id, cost_microusd,
                                          latency_ms)
                   VALUES ('twin.answer', 'v1', :m, 'ok', :u, :r, :cost, 1)"""
            ),
            {"m": model, "u": w.candidate, "r": run, "cost": micro},
        )
