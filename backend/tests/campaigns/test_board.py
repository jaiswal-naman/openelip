"""The board operations (plan 07-04; AI_SYSTEM 6.2): each one is a unit of the caller's transaction."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from app.campaigns import board
from app.platform import db
from tests.campaigns.factories import make_run, make_world
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401
from tests.guardian.support import packet_payload


def test_open_step_returns_the_row_a_second_time_and_keeps_one(conn) -> None:
    world = make_world(conn)
    first = board.open_step(conn, world.campaign, "research", "researcher")
    again = board.open_step(conn, world.campaign, "research", "researcher")
    assert again.id == first.id and first.status == "pending" and not first.ended
    assert conn.execute(text("SELECT count(*) FROM campaign_steps")).scalar_one() == 1


def test_note_merges_ids_and_complete_and_fail_end_the_step(conn) -> None:
    world = make_world(conn)
    step = board.open_step(conn, world.campaign, "packet_draft", "writer")
    board.start(conn, step)
    board.note(conn, step.id, {"tailored_resume_id": "t-1"})
    board.note(conn, step.id, {"packet_id": "p-1"})
    noted = board.get_step(conn, world.campaign, "packet_draft")
    assert (
        noted is not None
        and noted.status == "running"
        and noted.output == {"tailored_resume_id": "t-1", "packet_id": "p-1"}
    )
    board.complete(conn, step.id, {"extra": 1})
    done = board.get_step(conn, world.campaign, "packet_draft")
    assert done is not None and done.ended and done.finished_at is not None
    assert done.output == {"tailored_resume_id": "t-1", "packet_id": "p-1", "extra": 1}
    other = board.open_step(conn, world.campaign, "fit", "analyst")
    board.fail(conn, other.id, "ai_disabled")
    failed = board.get_step(conn, world.campaign, "fit")
    assert failed is not None and failed.status == "failed" and failed.error_code == "ai_disabled" and failed.ended


def test_wait_until_sets_the_step_waiting_and_enqueues_one_wake_job_at_that_time(conn) -> None:
    world = make_world(conn)
    step = board.open_step(conn, world.campaign, "follow_up:1", "chaser")
    at = datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)
    board.wait_until(conn, step, world.user, at)
    board.wait_until(conn, step, world.user, at)  # the same time again: no second job
    waiting = board.get_step(conn, world.campaign, "follow_up:1")
    assert waiting is not None and waiting.status == "waiting" and waiting.due_at == at
    jobs = conn.execute(text("SELECT unique_key, run_at, payload FROM queue_jobs WHERE kind = 'campaign.wake'")).all()
    assert len(jobs) == 1
    assert jobs[0].unique_key == f"campaign.wake:{world.campaign}:{at.isoformat()}" and jobs[0].run_at == at
    assert jobs[0].payload == {"campaign_id": str(world.campaign), "due_at": at.isoformat()}


def test_a_todo_is_cleared_when_the_step_waits_again_or_ends(conn) -> None:
    world = make_world(conn)
    step = board.open_step(conn, world.campaign, "follow_up:1", "chaser")
    board.wait_for_person(conn, step.id, {"todo": "add_follow_up_address"})
    got = board.get_step(conn, world.campaign, "follow_up:1")
    assert got is not None and got.status == "waiting" and got.output == {"todo": "add_follow_up_address"}
    board.wait_until(conn, step, world.user, datetime.now(UTC) + timedelta(days=1))
    got = board.get_step(conn, world.campaign, "follow_up:1")
    assert got is not None and "todo" not in got.output


@pytest.fixture
def committed(engine: Engine, clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch)
    return w.build_world(engine)


def test_propose_records_the_action_and_sets_the_step_waiting_on_its_approval_in_one_transaction(
    engine: Engine, committed
) -> None:
    world = committed
    payload = packet_payload(world.job_id)
    provenance = {"fields": {}, "sentences": [], "code_parts": [], "dossier_id": None, "input_flags": []}
    with db.transaction(world.user, role="worker") as conn:
        step = board.open_step(conn, world.campaign, "packet", "operator")
        run = make_run(conn, world.user, world.campaign, "operator", campaign_step_id=step.id)
        proposed = board.propose(
            conn,
            step,
            run,
            action_type="submit_application",
            payload=payload,
            provenance=provenance,
            step_id="packet.0",
        )
        again = board.propose(
            conn,
            step,
            run,
            action_type="submit_application",
            payload=payload,
            provenance=provenance,
            step_id="packet.0",
        )
    assert proposed.status == "awaiting_approval" and proposed.approval_id is not None
    assert again.action_id == proposed.action_id  # the same step id finds the action already made
    with engine.connect() as c:
        row = c.execute(
            text("SELECT status::text, waiting_for_approval_id, output FROM campaign_steps WHERE step_key = 'packet'")
        ).one()
        assert c.execute(text("SELECT count(*) FROM agent_actions WHERE kind = 'outside_action'")).scalar_one() == 1
    assert row.status == "waiting" and row.waiting_for_approval_id == proposed.approval_id
    assert row.output == {"action_id": proposed.action_id, "approval_id": str(proposed.approval_id)}
