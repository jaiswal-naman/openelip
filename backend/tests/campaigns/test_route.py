"""`route` (plan 07-04; ARCHITECTURE 5.9 rule 4): the one node that reads the whole campaign, names the next node and
moves the stage. Table-driven: each case builds rows, calls route and checks the answer and what it changed. No model
is called."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.campaigns import board, nodes, stages
from app.hiring import api as hiring
from app.platform import db
from tests.campaigns.factories import make_action, make_approval, make_campaign, make_check, make_run
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401
from tests.hiring import factories as hf
from tests.p5_factories import make_packet

ROLES = {
    "research": "researcher",
    "fit": "analyst",
    "packet_draft": "writer",
    "packet": "operator",
    "interview_prep": "coach",
}


@pytest.fixture(autouse=True)
def fakes(clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch)


def go(world: w.World) -> str:
    return nodes.route({"campaign_id": str(world.campaign), "next": None, "cause": "test"})["next"]


def step(engine: Engine, world: w.World, key: str, status: str = "done", **fields: Any) -> None:
    role = ROLES.get(key, "chaser")
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO campaign_steps (campaign_id, user_id, step_key, agent_role, status, output, due_at,
                                               finished_at, waiting_for_approval_id)
                   VALUES (:c, :u, :k, CAST(:r AS agent_role), CAST(:s AS step_status), CAST(:o AS jsonb),
                           COALESCE(:due, now()), :fin, :appr)"""
            ),
            {
                "c": world.campaign,
                "u": world.user,
                "k": key,
                "r": role,
                "s": status,
                "o": json.dumps(fields.get("output", {})),
                "due": fields.get("due_at"),
                "fin": fields.get("finished_at") or (datetime.now(UTC) if status in ("done", "failed") else None),
                "appr": fields.get("approval"),
            },
        )


def done(engine: Engine, world: w.World, *keys: str) -> None:
    for key in keys:
        step(engine, world, key)


def set_stage(engine: Engine, world: w.World, to: str) -> None:
    with db.transaction(world.user, role="worker") as conn:
        assert stages.move(conn, world.campaign, to, cause="setup", wake=False)


def packet_action(engine: Engine, world: w.World, status: str, *, outcome: str | None = None) -> tuple[int, Any]:
    with engine.begin() as c:
        run = make_run(c, world.user, world.campaign, "operator")
        action = make_action(c, run, action_type="submit_application", status=status, outcome_code=outcome)
        for kind in ("citations", "consent", "scope", "budget", "injection", "rate_limit"):
            make_check(c, action, kind, True)
        approval = make_approval(c, action, status="pending" if status == "awaiting_approval" else "approved")
    step(
        engine,
        world,
        "packet",
        "waiting",
        output={"action_id": action, "approval_id": str(approval)},
        approval=approval,
    )
    return action, approval


def moves(engine: Engine, world: w.World) -> list[str]:
    return [
        r.outcome_code
        for r in w.rows(
            engine,
            "SELECT outcome_code FROM agent_actions WHERE campaign_id = :c AND kind = 'state_change' ORDER BY id",
            c=world.campaign,
        )
    ]


@pytest.fixture
def world(engine: Engine) -> w.World:
    return w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)


# ---- the end, parking and the pauses
# -----------------------------------------------------------------------------------


def test_a_closed_campaign_ends_the_thread_and_asks_for_its_checkpoints_to_be_deleted(engine, world) -> None:
    with db.transaction(world.user, role="worker") as conn:
        stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="cancelled", wake=False)
    assert go(world) == "end"
    assert (
        w.one(
            engine, "SELECT count(*) FROM queue_jobs WHERE unique_key = :k", k=f"graph.delete:campaign:{world.campaign}"
        )
        == 1
    )
    assert go(world) == "end" and w.one(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'graph.delete'") == 1


def test_a_paused_campaign_and_an_inactive_account_park(engine, world) -> None:
    with db.transaction(world.user, role="worker") as conn:
        stages.pause(conn, world.campaign, "candidate", wake=False)
    assert go(world) == "park"
    with db.transaction(world.user, role="worker") as conn:
        stages.unpause(conn, world.campaign, by="candidate", wake=False)
    assert go(world) == "research"
    with engine.begin() as c:
        c.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": world.user})
    assert go(world) == "park"


def test_a_scam_signal_pauses_a_researching_campaign_once_per_content_version(engine) -> None:
    world = w.build_world(engine, description=w.POSTING + " You must pay a registration fee before the interview.")
    assert go(world) == "park"
    assert moves(engine, world) == ["scam_suspected"]
    signal = w.one(engine, "SELECT inputs FROM agent_actions WHERE kind = 'state_change'")
    assert signal["signals"][0]["quote"] and signal["content_version"] == 1
    assert go(world) == "park"  # still paused: nothing more written
    with db.transaction(world.user, role="worker") as conn:  # the candidate's override
        stages.unpause(conn, world.campaign, by="candidate", wake=False)
    assert go(world) == "research"
    assert moves(engine, world) == ["scam_suspected", "unpaused"]
    with engine.begin() as c:  # the posting changed: a new content version is read again
        c.execute(text("UPDATE jobs SET content_version = 2 WHERE id = :j"), {"j": world.job_id})
    assert go(world) == "park"
    assert moves(engine, world)[-1] == "scam_suspected"


def test_a_clean_posting_is_never_paused_for_a_scam(engine, world) -> None:
    assert go(world) == "research" and moves(engine, world) == []


def test_a_job_closed_before_applied_closes_the_campaign_with_job_closed(engine, world) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": world.job_id})
    assert go(world) == "end"
    assert w.stage(engine, world) == "closed"
    assert w.one(engine, "SELECT closed_reason::text FROM campaigns WHERE id = :c", c=world.campaign) == "job_closed"


def test_a_closed_job_after_applied_leaves_the_campaign_alone(engine, world) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "applied")
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": world.job_id})
    assert go(world) != "end" and w.stage(engine, world) == "applied"


def test_a_model_node_that_would_pass_the_budget_pauses_the_campaign(engine) -> None:
    world = w.build_world(engine, budget_microusd=100_000)
    assert go(world) == "park"
    assert moves(engine, world) == ["budget_exhausted"]
    assert w.one(engine, "SELECT paused_at IS NOT NULL FROM campaigns WHERE id = :c", c=world.campaign)


def test_the_fit_node_uses_no_model_so_it_runs_on_a_nearly_spent_budget(engine) -> None:
    world = w.build_world(engine, budget_microusd=100_000)
    done(engine, world, "research")
    assert go(world) == "fit" and moves(engine, world) == []


# ---- order
# -----------------------------------------------------------------------------------------------------------------


def test_research_fit_packet_draft_and_packet_come_in_order(engine, world) -> None:
    assert go(world) == "research"
    done(engine, world, "research")
    assert go(world) == "fit"
    done(engine, world, "fit")
    assert go(world) == "packet_draft"
    done(engine, world, "packet_draft")
    assert go(world) == "packet"


def test_a_pending_packet_approval_moves_the_stage_to_needs_you_and_parks(engine, world) -> None:
    done(engine, world, "research", "fit", "packet_draft")
    packet_action(engine, world, "awaiting_approval")
    assert go(world) == "park"
    assert w.stage(engine, world) == "needs_you" and moves(engine, world) == ["packet_waiting"]
    assert go(world) == "park" and moves(engine, world) == ["packet_waiting"]  # once


def test_an_approved_packet_is_marked_approved_and_its_step_is_closed_by_the_node(engine, world) -> None:
    with engine.begin() as c:
        job = SimpleNamespace(id=world.job_id)
        packet = make_packet(c, world.user, job)  # type: ignore[arg-type]
    done(engine, world, "research", "fit")
    step(engine, world, "packet_draft", output={"packet_id": str(packet)})
    packet_action(engine, world, "scheduled")
    assert go(world) == "packet"  # the node completes the step
    assert w.one(engine, "SELECT status::text FROM application_packets WHERE id = :p", p=packet) == "approved"
    assert w.stage(engine, world) == "researching"  # still researching only until the pending one was seen


def test_a_rejected_packet_waits_for_the_candidate(engine, world) -> None:
    done(engine, world, "research", "fit", "packet_draft")
    packet_action(engine, world, "rejected")
    assert go(world) == "park"


def test_a_packet_blocked_by_a_pause_or_expired_is_proposed_again(engine, world) -> None:
    done(engine, world, "research", "fit", "packet_draft")
    packet_action(engine, world, "blocked", outcome="campaign_paused")
    assert go(world) == "packet"


def test_a_packet_blocked_for_another_reason_parks(engine, world) -> None:
    done(engine, world, "research", "fit", "packet_draft")
    packet_action(engine, world, "blocked", outcome="restricted_data")
    assert go(world) == "park"


# ---- what the candidate and the employer record
# ---------------------------------------------------------------------------


def test_applied_marks_the_packet_action_executed_and_moves_to_applied(engine, world) -> None:
    done(engine, world, "research", "fit", "packet_draft")
    action, _ = packet_action(engine, world, "scheduled")
    w.record_application(engine, world, "applied")
    nxt = go(world)
    assert (
        w.stage(engine, world) == "applied"
        and w.one(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=action) == "executed"
    )
    assert nxt in ("follow_up", "park") and moves(engine, world) == ["application_applied"]


@pytest.mark.parametrize(
    ("status", "stage", "node"), [("interview", "interview", "interview_prep"), ("offer", "offer", "park")]
)
def test_interview_and_offer_move_there(engine, world, status, stage, node) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "applied")
    w.record_application(engine, world, status)
    assert go(world) == node and w.stage(engine, world) == stage


@pytest.mark.parametrize("status", ["rejected", "no_response"])
def test_rejected_and_no_response_close_with_that_reason(engine, world, status) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "applied")
    w.record_application(engine, world, status)
    assert go(world) == "end"
    assert w.one(engine, "SELECT closed_reason::text FROM campaigns WHERE id = :c", c=world.campaign) == status


def test_a_stage_never_moves_backwards(engine, world) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "interview")
    w.record_application(engine, world, "applied")
    go(world)
    assert w.stage(engine, world) == "interview"


def test_every_stage_change_is_a_state_change_row_under_the_lock_version(engine, world) -> None:
    done(engine, world, "research")
    before = w.one(engine, "SELECT lock_version FROM campaigns WHERE id = :c", c=world.campaign)
    set_stage(engine, world, "applied")
    w.record_application(engine, world, "interview")
    go(world)
    after = w.one(engine, "SELECT lock_version FROM campaigns WHERE id = :c", c=world.campaign)
    assert after == before + 2 and moves(engine, world) == ["setup", "application_interview"]
    assert (
        w.one(
            engine, "SELECT count(*) FROM agent_actions WHERE kind = 'state_change' AND inputs ->> 'to' = 'interview'"
        )
        == 1
    )


# ---- follow-ups
# -----------------------------------------------------------------------------------------------------------


def applied_world(engine: Engine, world: w.World, *, quiet_days: int) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "applied")
    with engine.begin() as c:
        c.execute(
            text("UPDATE campaigns SET last_update_at = now() - make_interval(days => :d) WHERE id = :c"),
            {"d": quiet_days, "c": world.campaign},
        )


def due_of(engine: Engine, world: w.World, days: int = 7) -> datetime:
    return w.one(engine, "SELECT last_update_at FROM campaigns WHERE id = :c", c=world.campaign) + timedelta(days=days)


def test_follow_up_one_is_planned_then_waits_then_is_due(engine, world) -> None:
    applied_world(engine, world, quiet_days=8)
    assert go(world) == "follow_up"  # no step yet: the node creates it (due, so it drafts)
    due = due_of(engine, world)
    with db.transaction(world.user, role="worker") as conn:
        s = board.open_step(conn, world.campaign, "follow_up:1", "chaser")
        board.wait_until(conn, s, world.user, due)
    assert go(world) == "follow_up"  # waiting, and the day has come
    applied_world_later = datetime.now(UTC) + timedelta(days=3)
    with engine.begin() as c:
        c.execute(
            text("UPDATE campaigns SET last_update_at = :t WHERE id = :c"),
            {"t": applied_world_later, "c": world.campaign},
        )
    assert go(world) == "follow_up"  # later news: the due time moved, the node waits again at the new time
    with db.transaction(world.user, role="worker") as conn:
        board.wait_until(
            conn, board.open_step(conn, world.campaign, "follow_up:1", "chaser"), world.user, due_of(engine, world)
        )
    assert go(world) == "park"  # waiting for a future day: park


def test_follow_up_two_is_a_week_after_the_first_ended_and_there_is_never_a_third(engine, world) -> None:
    applied_world(engine, world, quiet_days=30)
    ended = datetime.now(UTC) - timedelta(days=1)
    step(engine, world, "follow_up:1", "done", finished_at=ended)
    assert go(world) == "follow_up"  # follow_up:2 has no step yet
    with db.transaction(world.user, role="worker") as conn:
        s = board.open_step(conn, world.campaign, "follow_up:2", "chaser")
        board.wait_until(conn, s, world.user, ended + timedelta(days=7))
        assert s.step_key == "follow_up:2"
    assert go(world) == "park"  # a future day
    with engine.begin() as c:
        c.execute(text("UPDATE campaign_steps SET status = 'done', finished_at = now() WHERE step_key = 'follow_up:2'"))
    assert go(world) == "park"  # both ended: never a third
    assert w.one(engine, "SELECT count(*) FROM campaign_steps WHERE step_key LIKE 'follow_up:%'") == 2


def test_a_follow_up_step_waiting_for_an_address_parks_until_one_is_typed(engine) -> None:
    world = w.build_world(engine)  # no address
    applied_world(engine, world, quiet_days=9)
    step(
        engine, world, "follow_up:1", "waiting", output={"todo": "add_follow_up_address"}, due_at=due_of(engine, world)
    )
    assert go(world) == "park"
    with engine.begin() as c:
        c.execute(
            text("UPDATE campaigns SET follow_up_email = :e WHERE id = :c"), {"e": w.TYPED_ADDRESS, "c": world.campaign}
        )
    assert go(world) == "follow_up"


def test_a_follow_up_waiting_for_its_approval_parks_and_a_decided_one_goes_back_to_the_node(engine, world) -> None:
    applied_world(engine, world, quiet_days=9)
    with engine.begin() as c:
        run = make_run(c, world.user, world.campaign, "chaser")
        action = make_action(c, run, action_type="send_follow_up", status="awaiting_approval")
        approval = make_approval(c, action)
    step(engine, world, "follow_up:1", "waiting", output={"action_id": action}, approval=approval)
    assert go(world) == "park"
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(
            text("UPDATE agent_actions SET status = 'rejected', outcome_code = 'rejected' WHERE id = :a"), {"a": action}
        )
    assert go(world) == "follow_up"


def test_interview_prep_runs_once(engine, world) -> None:
    done(engine, world, "research")
    set_stage(engine, world, "interview")
    assert go(world) == "interview_prep"
    done(engine, world, "interview_prep")
    assert go(world) == "park"


def test_the_follow_up_plan_is_pure_and_never_plans_a_third() -> None:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    long_ago = now - timedelta(days=30)
    ended = {
        f"follow_up:{n}": board.Step(None, None, f"follow_up:{n}", "done", now, {}, None, None, now - timedelta(days=1))  # type: ignore[arg-type]
        for n in (1, 2)
    }
    plan = nodes.follow_up_plan(ended, {}, last_update_at=long_ago, days=7, now=now, has_address=True)
    assert plan.todo == "none"
    first = nodes.follow_up_plan({}, {}, last_update_at=long_ago, days=7, now=now, has_address=True)
    assert (first.todo, first.n, first.due) == ("node", 1, long_ago + timedelta(days=7))


# ---- an introduction campaign
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def intro(engine: Engine) -> w.World:
    with engine.begin() as c:
        org = hf.make_org(c)
        role = hf.make_open_role(c, org)
        candidate = hf.make_candidate(c)
        accepted = hf.make_accepted(c, role, candidate)
        assert role.job_id is not None
        campaign = make_campaign(c, candidate.id, role.job_id, stage="applied", introduction_id=accepted.id)
        c.execute(text("UPDATE jobs SET apply_url = :u WHERE id = :j"), {"u": w.APPLY_URL, "j": role.job_id})
    return w.World(candidate.id, role.job_id, campaign, {})


def answer(monkeypatch, value: str | None) -> None:
    real = hiring.introduction_ref

    def ref(conn, introduction_id):  # type: ignore[no-untyped-def]
        found = real(conn, introduction_id)
        return None if found is None else found.__class__(**{**found.__dict__, "employer_answer": value})

    monkeypatch.setattr(nodes.hiring, "introduction_ref", ref)


def test_an_introduction_campaign_has_only_research_and_interview_prep(engine, intro) -> None:
    assert go(intro) == "research"
    done(engine, intro, "research")
    assert go(intro) == "park"  # no fit, no packet, no follow-up
    set_stage(engine, intro, "interview")
    assert go(intro) == "interview_prep"


def test_an_employer_yes_moves_applied_to_talking_and_a_no_closes_it_as_rejected(engine, intro, monkeypatch) -> None:
    done(engine, intro, "research")
    answer(monkeypatch, "yes")
    assert go(intro) == "park" and w.stage(engine, intro) == "talking"
    assert moves(engine, intro) == ["employer_yes"]


def test_an_employer_no_closes_the_campaign_as_rejected(engine, intro, monkeypatch) -> None:
    done(engine, intro, "research")
    answer(monkeypatch, "no")
    assert go(intro) == "end"
    assert w.one(engine, "SELECT closed_reason::text FROM campaigns WHERE id = :c", c=intro.campaign) == "rejected"
