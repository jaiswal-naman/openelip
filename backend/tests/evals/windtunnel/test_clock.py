"""The virtual clock: one campaign's waiting moves, and nothing else does."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.evals.windtunnel import clock
from tests.campaigns.factories import make_action, make_approval, make_campaign, make_run, make_step, make_world
from tests.evals.windtunnel.conftest import empty


@pytest.fixture(autouse=True)
def tables(engine):
    empty(engine)
    yield
    empty(engine)


def wake(conn, campaign, at, status="queued"):
    conn.execute(
        text(
            """INSERT INTO queue_jobs (queue, kind, payload, unique_key, run_at, status)
               VALUES ('default', 'campaign.wake', CAST(:p AS jsonb), :k, :at, CAST(:s AS queue_status))"""
        ),
        {
            "p": f'{{"campaign_id": "{campaign}"}}',
            "k": f"campaign.wake:{campaign}:{at.isoformat()}:{status}",
            "at": at,
            "s": status,
        },
    )


def snapshot(conn, campaign):
    return conn.execute(
        text(
            """SELECT c.last_update_at, c.stage::text, c.updated_at IS NOT NULL,
                      (SELECT array_agg(due_at ORDER BY step_key) FROM campaign_steps WHERE campaign_id = c.id),
                      (SELECT array_agg(run_at ORDER BY unique_key) FROM queue_jobs
                        WHERE payload ->> 'campaign_id' = CAST(c.id AS text))
                 FROM campaigns c WHERE c.id = :c"""
        ),
        {"c": campaign},
    ).one()


def test_next_due_is_the_earliest_waiting_step_or_queued_wake(engine):
    now = datetime.now(UTC)
    with engine.begin() as conn:
        world = make_world(conn)
        assert clock.next_due(conn, world.campaign) is None
        make_step(conn, world.campaign, "research", status="done", due_at=now)
        make_step(conn, world.campaign, "follow_up:1", status="waiting", due_at=now + timedelta(days=7))
        make_step(conn, world.campaign, "follow_up:2", status="pending", due_at=now + timedelta(days=1))  # not waiting
        wake(conn, world.campaign, now + timedelta(days=5))
        wake(conn, world.campaign, now + timedelta(days=2), status="succeeded")  # not queued
        due = clock.next_due(conn, world.campaign)
        assert due is not None and abs(due - (now + timedelta(days=5))) < timedelta(seconds=1)
        assert clock.next_due(conn, world.campaign, after=now + timedelta(days=6)) == pytest.approx(
            now + timedelta(days=7), abs=timedelta(seconds=1)
        )
        assert clock.next_due(conn, world.campaign, after=now + timedelta(days=8)) is None


def test_next_due_ignores_other_campaigns(engine):
    now = datetime.now(UTC)
    with engine.begin() as conn:
        mine, other = make_world(conn), make_world(conn)
        make_step(conn, other.campaign, "follow_up:1", status="waiting", due_at=now + timedelta(days=1))
        assert clock.next_due(conn, mine.campaign) is None


def test_shift_moves_exactly_the_three_columns_of_one_campaign(engine):
    now = datetime.now(UTC)
    with engine.begin() as conn:
        mine, other = make_world(conn), make_world(conn)
        for world in (mine, other):
            make_step(conn, world.campaign, "follow_up:1", status="waiting", due_at=now + timedelta(days=7))
            make_step(conn, world.campaign, "research", status="done", due_at=now)
            wake(conn, world.campaign, now + timedelta(days=7))
        run = make_run(conn, mine.user, mine.campaign, "chaser")
        action = make_action(conn, run, kind="outside_action", action_type="send_follow_up", status="proposed")
        make_approval(conn, action, status="pending")
        undo_before = conn.execute(
            text("SELECT undo_until, status::text FROM agent_actions WHERE id = :a"), {"a": action}
        ).one()
        approval_before = conn.execute(
            text("SELECT expires_at, status::text FROM approvals WHERE agent_action_id = :a"), {"a": action}
        ).one()
        before, other_before = snapshot(conn, mine.campaign), snapshot(conn, other.campaign)
        clock.shift(conn, mine.campaign, timedelta(days=8))
        after = snapshot(conn, mine.campaign)
        assert after[0] == before[0] - timedelta(days=8)
        waiting_before = conn.execute(
            text("SELECT due_at FROM campaign_steps WHERE campaign_id = :c AND step_key = 'follow_up:1'"),
            {"c": mine.campaign},
        ).scalar_one()
        assert abs(waiting_before - (now - timedelta(days=1))) < timedelta(seconds=2)
        assert after[4][0] == before[4][0] - timedelta(days=8)
        assert snapshot(conn, other.campaign) == other_before  # another campaign: not one column moved
        assert (
            conn.execute(text("SELECT undo_until, status::text FROM agent_actions WHERE id = :a"), {"a": action}).one()
            == undo_before
        )
        assert (
            conn.execute(
                text("SELECT expires_at, status::text FROM approvals WHERE agent_action_id = :a"), {"a": action}
            ).one()
            == approval_before
        )
        done_step = conn.execute(
            text("SELECT due_at FROM campaign_steps WHERE campaign_id = :c AND step_key = 'research'"),
            {"c": mine.campaign},
        ).scalar_one()
        assert abs(done_step - now) < timedelta(seconds=2)  # a finished step is history, not waiting


def test_shift_moves_the_finish_time_of_finished_steps_of_that_campaign_only(engine):
    now = datetime.now(UTC)
    with engine.begin() as conn:
        mine, other = make_world(conn), make_world(conn)
        for world in (mine, other):
            make_step(conn, world.campaign, "follow_up:1", status="done", due_at=now)
            conn.execute(
                text("UPDATE campaign_steps SET finished_at = :at WHERE campaign_id = :c"),
                {"at": now, "c": world.campaign},
            )
        clock.shift(conn, mine.campaign, timedelta(days=7))
        finished = {
            w: conn.execute(text("SELECT finished_at FROM campaign_steps WHERE campaign_id = :c"), {"c": w}).scalar_one()
            for w in (mine.campaign, other.campaign)
        }
        assert abs(finished[mine.campaign] - (now - timedelta(days=7))) < timedelta(seconds=2)
        assert abs(finished[other.campaign] - now) < timedelta(seconds=2)


def test_a_virtual_clock_counts_real_time_and_every_shift_made_before_a_moment():
    started = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    virtual = clock.VirtualClock(started)
    assert virtual.day(started + timedelta(hours=12)) == pytest.approx(0.5)
    virtual.shifts.append((started + timedelta(hours=1), timedelta(days=7)))
    assert virtual.day(started + timedelta(minutes=30)) == pytest.approx(30 / 1440)
    assert virtual.day(started + timedelta(hours=2)) == pytest.approx(7 + 2 / 24)
    assert virtual.offset == timedelta(days=7)
    before = virtual.now()
    virtual.advance(timedelta(days=1))
    assert virtual.now() - before >= timedelta(days=1)
    assert make_campaign  # the factory module is the one Phase 7's tests use
