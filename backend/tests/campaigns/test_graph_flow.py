"""CMP-01 to CMP-07 as running behaviour (plan 07-04): campaigns driven from start to closed through the real graph, the
real queue, the real Guardian and the fake provider with recorded answers. Everything is invented."""

import pytest
from sqlalchemy import Engine, text

from app.campaigns import stages
from app.guardian import api as guardian
from app.guardian import runtime
from app.platform import db
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401  (the fixture that empties the tables around a committing test)


@pytest.fixture(autouse=True)
def graph_world(clean, monkeypatch):  # noqa: F811
    runtime.close()
    w.use_fakes(monkeypatch)
    yield
    runtime.close()


def action_of(engine: Engine, world: w.World, action_type: str):
    (action,) = w.rows(
        engine,
        """SELECT a.id, a.status::text AS status, a.payload, p.id AS approval_id FROM agent_actions a
           JOIN approvals p ON p.agent_action_id = a.id
           WHERE a.campaign_id = :c AND a.action_type = CAST(:t AS action_type) AND a.inputs ->> 'replaces' IS NULL""",
        c=world.campaign,
        t=action_type,
    )
    return action


def thread(engine: Engine, world: w.World) -> int:
    return w.one(
        engine, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t", t=f"campaign:{world.campaign}"
    )


def test_a_job_campaign_runs_from_researching_to_closed(engine: Engine) -> None:
    world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)
    w.resume(world)
    w.run_jobs()
    # research, fit, packet_draft and packet ran by themselves and the packet waits for the candidate
    assert w.steps(engine, world) == {
        "research": "done",
        "fit": "done",
        "packet_draft": "done",
        "packet": "waiting",
    }
    assert w.stage(engine, world) == "needs_you"
    assert thread(engine, world) > 0  # one parked thread
    assert w.one(engine, "SELECT count(*) FROM company_dossiers WHERE campaign_id = :c", c=world.campaign) == 1
    submit = action_of(engine, world, "submit_application")
    assert submit.status == "awaiting_approval"
    # one approval of the one action releases the kit and moves the packet to approved
    assert w.approve(world, submit.approval_id, submit.payload) == "scheduled"
    w.resume(world, f"a{submit.id}.scheduled")
    w.run_jobs()
    assert w.steps(engine, world)["packet"] == "done"
    assert (
        w.one(
            engine,
            "SELECT status::text FROM application_packets WHERE campaign_id = :c AND status <> 'superseded'",
            c=world.campaign,
        )
        == "approved"
    )
    assert w.stage(engine, world) == "needs_you"  # the candidate still has to submit it themselves
    # the candidate records that they applied: applied, the action executed, follow_up:1 waiting for its day
    w.record_application(engine, world, "applied")
    w.resume(world, "app1")
    w.run_jobs()
    assert w.stage(engine, world) == "applied"
    assert w.one(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=submit.id) == "executed"
    assert w.steps(engine, world)["follow_up:1"] == "waiting"
    wake = w.rows(engine, "SELECT run_at, status::text AS status FROM queue_jobs WHERE kind = 'campaign.wake'")
    assert len(wake) == 1 and wake[0].status == "queued"
    assert (
        w.one(
            engine,
            "SELECT count(*) FROM agent_actions WHERE campaign_id = :c AND action_type = 'send_follow_up'",
            c=world.campaign,
        )
        == 0
    )
    # eight quiet days later the wake job runs and the Chaser proposes one follow-up to the typed address
    w.age(engine, world, 8)
    w.run_jobs()
    follow = action_of(engine, world, "send_follow_up")
    assert follow.status == "awaiting_approval"
    assert follow.payload["to"] == w.TYPED_ADDRESS
    assert follow.payload["reply_to"].endswith("@example.test") or "@" in follow.payload["reply_to"]
    assert w.approve(world, follow.approval_id, follow.payload) == "scheduled"
    w.resume(world, f"a{follow.id}.scheduled")
    w.run_jobs()
    assert w.steps(engine, world)["follow_up:1"] == "done"
    assert w.steps(engine, world)["follow_up:2"] == "waiting"  # the second chase is a week on, never a third
    # interview: the Coach prepares once
    w.record_application(engine, world, "interview")
    w.resume(world, "app2")
    w.run_jobs()
    assert w.stage(engine, world) == "interview"
    assert w.steps(engine, world)["interview_prep"] == "done"
    prep = w.one(
        engine,
        "SELECT output FROM campaign_steps WHERE campaign_id = :c AND step_key = 'interview_prep'",
        c=world.campaign,
    )
    assert prep["questions"] and prep["ask_them"][0]["claim_indexes"] == [0]
    # offer, then the candidate closes it: the last route ends the thread and its checkpoints are deleted
    w.record_application(engine, world, "offer")
    w.resume(world, "app3")
    w.run_jobs()
    assert w.stage(engine, world) == "offer"
    with db.transaction(world.user, role="worker") as conn:
        assert stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="offer_accepted")
    w.run_jobs()
    assert w.stage(engine, world) == "closed"
    assert thread(engine, world) == 0
    # the recorder lists every step, in order, with every kind of row
    with db.transaction(world.user) as conn:
        entries = guardian.timeline(conn, world.user, campaign_id=world.campaign)
    kinds = [e["kind"] for e in entries]
    assert {"model_call", "tool_call", "state_change", "outside_action"} <= set(kinds)
    roles = [e["agent_role"] for e in entries if e["kind"] in ("model_call", "outside_action")]
    assert roles.index("researcher") < roles.index("writer") < roles.index("operator") < roles.index("chaser")
    moves = [e["outcome_code"] for e in entries if e["kind"] == "state_change"]
    assert moves[:3] == ["packet_waiting", "application_applied", "application_interview"]
    assert moves[-1] == "offer_accepted"
    # model spend is charged to the campaign
    assert w.one(engine, "SELECT count(*) FROM llm_calls WHERE campaign_id = :c", c=world.campaign) > 5


def test_pause_stops_work_and_resume_restarts_it(engine: Engine) -> None:
    world = w.build_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        stages.pause(conn, world.campaign, "candidate")
    w.run_jobs()
    assert w.steps(engine, world) == {}  # a paused campaign runs no step
    assert w.one(engine, "SELECT count(*) FROM llm_calls") == 0
    with db.transaction(world.user, role="worker") as conn:
        stages.unpause(conn, world.campaign, by="candidate")
    w.run_jobs()
    assert w.steps(engine, world)["research"] == "done"
    assert w.stage(engine, world) == "needs_you"


def test_cancel_ends_the_thread_and_runs_nothing_more(engine: Engine) -> None:
    world = w.build_world(engine)
    w.resume(world)
    w.run_jobs()
    assert thread(engine, world) > 0
    with db.transaction(world.user, role="worker") as conn:
        stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="cancelled")
    w.run_jobs()
    assert thread(engine, world) == 0
    before = w.one(engine, "SELECT count(*) FROM agent_actions WHERE campaign_id = :c", c=world.campaign)
    w.resume(world, "again")
    w.run_jobs()
    assert w.one(engine, "SELECT count(*) FROM agent_actions WHERE campaign_id = :c", c=world.campaign) == before


def test_a_campaign_over_budget_pauses_before_the_next_model_step(engine: Engine) -> None:
    world = w.build_world(engine, budget_microusd=100_000)  # below the research estimate of 300000
    w.resume(world)
    w.run_jobs()
    assert w.steps(engine, world) == {}
    with engine.connect() as c:
        reason = c.execute(
            text("SELECT outcome_code FROM agent_actions WHERE campaign_id = :c AND kind = 'state_change'"),
            {"c": world.campaign},
        ).scalar_one()
    assert reason == "budget_exhausted"
    assert w.one(engine, "SELECT paused_at IS NOT NULL FROM campaigns WHERE id = :c", c=world.campaign)


def test_a_scam_posting_pauses_before_research(engine: Engine) -> None:
    world = w.build_world(
        engine, description=w.POSTING + " Candidates must pay a registration fee of 50 dollars before the interview."
    )
    w.resume(world)
    w.run_jobs()
    assert w.steps(engine, world) == {}
    row = w.rows(
        engine,
        "SELECT outcome_code, inputs FROM agent_actions WHERE campaign_id = :c AND kind = 'state_change'",
        c=world.campaign,
    )[0]
    assert row.outcome_code == "scam_suspected" and row.inputs["signals"][0]["quote"]
    # the candidate's override: resuming is not paused again for the same posting
    with db.transaction(world.user, role="worker") as conn:
        stages.unpause(conn, world.campaign, by="candidate")
    w.run_jobs()
    assert w.steps(engine, world)["research"] == "done"
    assert w.one(engine, "SELECT paused_at IS NULL FROM campaigns WHERE id = :c", c=world.campaign)
