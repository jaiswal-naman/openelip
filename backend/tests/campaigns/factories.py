"""Invented rows for the Phase 7 tests (07-01): campaigns, steps, runs, recorder rows, approvals, Guardian checks and
dossiers, each built so that every database guard accepts it.

    from tests.campaigns.factories import (
        make_campaign, make_step, make_run, make_action, make_check, make_approval, make_dossier, make_world,
    )

    world = make_world(conn)                              # a person, an open job and a researching campaign
    run = make_run(conn, world.user, world.campaign, "operator")
    action = make_action(conn, run, kind="outside_action", action_type="submit_application", status="proposed")
    make_check(conn, action, "scope", True)
    make_approval(conn, action, status="approved")

Everything is made up. Users, jobs, decisions and introductions come from tests/match/factories.py and
tests/hiring/factories.py; write with plain SQL on the connection the test gives, which is the bootstrap login of the
`conn` fixture (row-level security does not apply to it). Call `act_as` after building rows, never before.

How the guards are treated:
  * A recorder row is inserted the way the worker inserts it, as `recorded` (model and tool calls, state changes) or
    `proposed` (outside actions); the database refuses any other first status. A factory asked for a later status moves
    the row there with `app.maintenance` set for that one statement, which the trigger lets through the way erase_user
    uses it, so the test starts from a row in that state without building both keys. Tests of the keys themselves
    (tests/db/test_two_key_schema.py and the Guardian tests) do not use this shortcut: they move rows with no
    maintenance flag and see the trigger refuse or accept.
  * An approval is inserted pending and decided by an UPDATE, which approvals_decide_once checks.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import Connection, text

from tests.match.factories import Job, make_job, make_user

OUTSIDE_PAYLOADS: dict[str, dict[str, Any]] = {
    "submit_application": {
        "job_id": 0,
        "apply_url": "https://careers.initech.example/apply",
        "packet_id": None,
        "tailored_resume_id": None,
        "answers": [],
        "cover_note": "A short invented note.",
    },
    "send_follow_up": {
        "channel": "email",
        "to": "talent@lumen.example",
        "reply_to": "ada@example.test",
        "subject": "Following up on my application",
        "body": "Hello, I applied last week and remain interested.",
    },
}


class World(NamedTuple):
    """A person with an open job and a campaign for it."""

    user: UUID
    job: Job
    campaign: UUID


def _require_test_database(conn: Connection) -> None:
    name = conn.execute(text("SELECT current_database()")).scalar_one()
    if not str(name).endswith("_test"):
        raise RuntimeError(f"refusing to write invented rows into database {name!r}")


def make_campaign(
    conn: Connection,
    user_id: UUID,
    job_id: int,
    *,
    stage: str = "researching",
    introduction_id: UUID | None = None,
    budget_microusd: int = 750_000,
    follow_up_email: str | None = None,
    follow_up_after_days: int = 7,
    paused: bool = False,
) -> UUID:
    """A campaign. A `closed` one gets a reason of `cancelled` and a closing time, as the stage CHECK requires."""
    _require_test_database(conn)
    return conn.execute(
        text(
            """INSERT INTO campaigns (user_id, job_id, introduction_id, stage, closed_reason, closed_at,
                                      budget_microusd, follow_up_email, follow_up_after_days, paused_at)
               VALUES (:u, :j, :i, CAST(:stage AS campaign_stage),
                       CASE WHEN :stage = 'closed' THEN 'cancelled'::campaign_closed_reason END,
                       CASE WHEN :stage = 'closed' THEN now() END, :budget, :email, :days,
                       CASE WHEN :paused THEN now() END)
               RETURNING id"""
        ),
        {
            "u": user_id,
            "j": job_id,
            "i": introduction_id,
            "stage": stage,
            "budget": budget_microusd,
            "email": follow_up_email,
            "days": follow_up_after_days,
            "paused": paused,
        },
    ).scalar_one()


def make_world(conn: Connection, *, stage: str = "researching", follow_up_email: str | None = None) -> World:
    """A new person, a new open job at an invented company and a campaign of that person for it."""
    user = make_user(conn)
    job = make_job(
        conn,
        title="Data engineer",
        description="Build Python pipelines.",
        requirements=[("Python pipelines", "required")],
        company_name="Lumen Demo",
    )
    return World(user, job, make_campaign(conn, user, job.id, stage=stage, follow_up_email=follow_up_email))


def make_step(
    conn: Connection,
    campaign_id: UUID,
    step_key: str,
    *,
    agent_role: str = "researcher",
    status: str = "pending",
    due_at: datetime | None = None,
    output: Mapping[str, Any] | None = None,
) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO campaign_steps (campaign_id, user_id, step_key, agent_role, status, due_at, output)
               VALUES (:c, (SELECT user_id FROM campaigns WHERE id = :c), :key, CAST(:role AS agent_role),
                       CAST(:status AS step_status), COALESCE(:due, now()), CAST(:output AS jsonb))
               RETURNING id"""
        ),
        {
            "c": campaign_id,
            "key": step_key,
            "role": agent_role,
            "status": status,
            "due": due_at,
            "output": None if output is None else json.dumps(output),
        },
    ).scalar_one()


def make_run(
    conn: Connection,
    user_id: UUID,
    campaign_id: UUID | None,
    role: str,
    *,
    campaign_step_id: UUID | None = None,
    config_version: str = "test-1",
) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO agent_runs (user_id, campaign_id, campaign_step_id, agent_role, config_version)
               VALUES (:u, :c, :s, CAST(:role AS agent_role), :v) RETURNING id"""
        ),
        {"u": user_id, "c": campaign_id, "s": campaign_step_id, "role": role, "v": config_version},
    ).scalar_one()


def make_action(
    conn: Connection,
    run_id: UUID,
    *,
    kind: str = "outside_action",
    action_type: str | None = None,
    status: str = "proposed",
    payload: Mapping[str, Any] | None = None,
    policy_decision: str | None = None,
    summary: str = "An invented step.",
    undo_until: datetime | None = None,
    outcome_code: str | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> int:
    """One recorder row of the run, with the next seq. An outside action gets `submit_application` and the payload
    of that type unless told otherwise, the policy decision `ask`, and policy version `test-1`."""
    _require_test_database(conn)
    outside = kind == "outside_action"
    if outside:
        action_type = action_type or "submit_application"
        payload = OUTSIDE_PAYLOADS[action_type] if payload is None else payload
        policy_decision = policy_decision or "ask"
    first = "proposed" if outside else "recorded"
    action_id: int = conn.execute(
        text(
            """INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, action_type, status, summary,
                                          inputs, payload, policy_decision, policy_version, outcome_code)
               SELECT r.id, r.user_id, r.campaign_id,
                      (SELECT coalesce(max(a.seq), 0) + 1 FROM agent_actions a WHERE a.agent_run_id = r.id),
                      CAST(:kind AS action_kind), CAST(:type AS action_type), CAST(:first AS action_status), :summary,
                      CAST(:inputs AS jsonb), CAST(:payload AS jsonb), CAST(:decision AS policy_decision),
                      CASE WHEN :outside THEN 'test-1' END,
                      CASE WHEN :status IN ('blocked', 'failed') THEN COALESCE(:code, 'test_code') ELSE :code END
               FROM agent_runs r WHERE r.id = :run RETURNING id"""
        ),
        {
            "run": run_id,
            "kind": kind,
            "type": action_type,
            "first": first,
            "status": status,
            "summary": summary,
            "inputs": json.dumps(dict(inputs or {})),
            "payload": None if payload is None else json.dumps(dict(payload)),
            "decision": policy_decision,
            "outside": outside,
            "code": outcome_code,
        },
    ).scalar_one()
    if status != first:
        _move(conn, action_id, status, undo_until)
    elif undo_until is not None:
        _move(conn, action_id, status, undo_until)
    return action_id


def _move(conn: Connection, action_id: int, status: str, undo_until: datetime | None) -> None:
    """Put the row into the wanted state under app.maintenance (see the module docstring)."""
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    try:
        conn.execute(
            text(
                """UPDATE agent_actions
                   SET status = CAST(:status AS action_status), undo_until = COALESCE(:undo, undo_until),
                       executed_at = CASE WHEN :status = 'executed' THEN now() END,
                       outcome_code = CASE WHEN :status IN ('blocked', 'failed', 'cancelled', 'rejected', 'expired')
                                           THEN COALESCE(outcome_code, 'test_code') ELSE outcome_code END
                   WHERE id = :id"""
            ),
            {"id": action_id, "status": status, "undo": undo_until},
        )
    finally:
        conn.execute(text("SELECT set_config('app.maintenance', '', true)"))


def make_check(
    conn: Connection,
    action_id: int,
    kind: str,
    passed: bool,
    reason_code: str | None = None,
    *,
    version: str = "test-1",
) -> int:
    """One Guardian verdict on an action. A failed one needs a reason code: `check_failed` when none is given."""
    _require_test_database(conn)
    return conn.execute(
        text(
            """INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, reason_code, guardian_version)
               SELECT a.id, a.user_id, CAST(:kind AS guardian_check_kind), :passed, :reason, :v
               FROM agent_actions a WHERE a.id = :id RETURNING id"""
        ),
        {
            "id": action_id,
            "kind": kind,
            "passed": passed,
            "reason": reason_code if reason_code or passed else "check_failed",
            "v": version,
        },
    ).scalar_one()


def make_approval(conn: Connection, action_id: int, *, status: str = "pending", expires_in: str = "7 days") -> UUID:
    """An approval of the action: inserted pending, then decided through the guard when `status` asks for it.
    `expires_in` is a PostgreSQL interval; a negative one makes an approval that has already expired."""
    _require_test_database(conn)
    approval: UUID = conn.execute(
        text(
            """INSERT INTO approvals (user_id, agent_action_id, expires_at)
               SELECT a.user_id, a.id, now() + CAST(:ttl AS interval) FROM agent_actions a WHERE a.id = :id
               RETURNING id"""
        ),
        {"id": action_id, "ttl": expires_in},
    ).scalar_one()
    if status != "pending":
        conn.execute(
            text(
                """UPDATE approvals SET status = CAST(:status AS approval_status), decided_at = now(),
                                        decided_via = CASE WHEN :status IN ('approved', 'rejected') THEN 'app' END
                   WHERE id = :id"""
            ),
            {"id": approval, "status": status},
        )
    return approval


def make_dossier(
    conn: Connection,
    campaign_id: UUID,
    *,
    claims: list[dict[str, Any]] | None = None,
    version: int = 1,
    company_name: str = "Lumen Demo",
) -> UUID:
    """A dossier with one cited claim unless `claims` is given."""
    claims = (
        [
            {
                "text": "Lumen Demo builds invented lighting software.",
                "sources": [
                    {
                        "url": "https://lumen.example/about",
                        "quote": "We build lighting software.",
                        "retrieved_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ]
        if claims is None
        else claims
    )
    return conn.execute(
        text(
            """INSERT INTO company_dossiers (campaign_id, user_id, company_name, version, claims)
               VALUES (:c, (SELECT user_id FROM campaigns WHERE id = :c), :name, :v, CAST(:claims AS jsonb))
               RETURNING id"""
        ),
        {"c": campaign_id, "name": company_name, "v": version, "claims": json.dumps(claims)},
    ).scalar_one()
