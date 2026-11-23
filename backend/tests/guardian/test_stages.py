"""Plan 07-03, CMP-07 and the stage contract: the one stage writer, on both logins. `pause`, `unpause` and the cancel
sequence run as app_api through db.transaction(user_id); `move` also as app_worker."""

import pytest
from sqlalchemy import text

from app.campaigns import stages
from app.guardian import api as guardian
from app.platform import db
from tests.guardian.support import approve_and_release, entails, make_committed_world, propose_follow_up, rows, scalar


def state_changes(engine):
    return [
        tuple(r)
        for r in rows(engine, "SELECT summary, outcome_code FROM agent_actions WHERE kind = 'state_change' ORDER BY id")
    ]


def test_move_as_the_worker_sets_the_stage_records_a_state_change_and_wakes_the_thread(engine, clean):
    world = make_committed_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        assert stages.move(conn, world.campaign, "needs_you", cause="a1.awaiting_approval") is True
        assert stages.move(conn, world.campaign, "needs_you", cause="again") is False  # already there
    assert scalar(engine, "SELECT stage::text FROM campaigns") == "needs_you"
    assert scalar(engine, "SELECT lock_version FROM campaigns") == 1
    assert state_changes(engine) == [("Stage moved from researching to needs_you.", "a1.awaiting_approval")]
    keys = [r[0] for r in rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume'")]
    assert keys == [f"campaign.resume:{world.campaign}:a1.awaiting_approval"]


def test_move_without_waking_and_a_lost_race_write_nothing(engine, clean):
    world = make_committed_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        assert stages.move(conn, world.campaign, "applied", cause="x", wake=False)
    assert scalar(engine, "SELECT count(*) FROM queue_jobs") == 0
    with db.transaction(world.user, role="worker") as conn:
        # another writer moves the stage between this one's read and its compare-and-set
        real = stages._read

        def stale(c, campaign_id):
            row = real(c, campaign_id)
            engine_conn = engine.connect()
            with engine_conn.begin():
                engine_conn.execute(text("UPDATE campaigns SET lock_version = lock_version + 1"))
            engine_conn.close()
            return row

        stages._read, saved = stale, real
        try:
            assert stages.move(conn, world.campaign, "talking", cause="race") is False
        finally:
            stages._read = saved
    assert scalar(engine, "SELECT stage::text FROM campaigns") == "applied"


def test_closing_needs_a_reason_sets_the_close_columns_and_clears_a_pause(engine, clean):
    world = make_committed_world(engine)
    with pytest.raises(ValueError):
        with db.transaction(world.user) as conn:
            stages.move(conn, world.campaign, "closed", cause="cancel")
    with db.transaction(world.user) as conn:
        assert stages.pause(conn, world.campaign, "candidate")
        assert stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="cancelled")
    row = rows(engine, "SELECT stage::text, closed_reason::text, closed_at IS NOT NULL, paused_at FROM campaigns")[0]
    assert tuple(row) == ("closed", "cancelled", True, None)
    assert state_changes(engine)[-1] == ("Stage moved from researching to closed.", "cancelled")
    with db.transaction(world.user) as conn:
        assert stages.move(conn, world.campaign, "researching", cause="reopen") is False  # closed stays closed


def test_pause_records_the_reason_with_quoted_signals_and_holds_scheduled_follow_ups(
    engine, clean, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    approve_and_release(world, p)
    signals = [{"kind": "fee_request", "quote": "Pay a small registration fee."}]
    with db.transaction(world.user) as conn:
        assert stages.pause(conn, world.campaign, "scam_suspected", details={"signals": signals})
        assert stages.pause(conn, world.campaign, "candidate") is False  # already paused
    assert scalar(engine, "SELECT paused_at IS NOT NULL FROM campaigns")
    change = rows(engine, "SELECT summary, outcome_code, inputs FROM agent_actions WHERE kind = 'state_change'")[0]
    assert (change[0], change[1]) == ("Campaign paused: scam_suspected.", "scam_suspected")
    assert change[2] == {"reason": "scam_suspected", "signals": signals}
    assert scalar(engine, "SELECT not_before::text FROM outbox") == "infinity"
    # the action events (proposal, release) also wake the campaign now that plan 07-05 subscribes to them
    keys = [r[0] for r in rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume'")]
    assert [k for k in keys if ":pause." in k] == [f"campaign.resume:{world.campaign}:pause.1"]
    with db.transaction(world.user) as conn:  # the gate now reads the reason
        gate = guardian_gate(conn, world.campaign)
    assert gate.paused_reason == "scam_suspected"


def guardian_gate(conn, campaign_id):
    from app.guardian import policy

    return policy.read_gate(conn, "campaign", campaign_id)


def test_unpause_releases_held_follow_ups_with_at_least_a_minute_left(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    approve_and_release(world, propose_follow_up(world))
    with db.transaction(world.user) as conn:
        stages.pause(conn, world.campaign, "candidate")
    with db.transaction(world.user) as conn:
        assert stages.unpause(conn, world.campaign, by="candidate")
        assert stages.unpause(conn, world.campaign, by="candidate") is False
    left = scalar(engine, "SELECT extract(epoch FROM not_before - clock_timestamp()) FROM outbox")
    assert float(left) >= 8 * 60  # the window of 10 minutes was still mostly open
    assert scalar(engine, "SELECT paused_at FROM campaigns") is None
    assert state_changes(engine)[-1] == ("Campaign resumed.", "unpaused")


def test_a_follow_up_whose_window_ended_during_a_pause_gets_a_minute_after_resume(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    approve_and_release(world, propose_follow_up(world))
    with db.transaction(world.user) as conn:
        stages.pause(conn, world.campaign, "candidate")
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("UPDATE agent_actions SET undo_until = now() - interval '1 hour' WHERE kind = 'outside_action'"))
    with db.transaction(world.user) as conn:
        stages.unpause(conn, world.campaign, by="candidate")
    left = float(scalar(engine, "SELECT extract(epoch FROM not_before - clock_timestamp()) FROM outbox"))
    assert 50 <= left <= 60


def test_the_cancel_sequence_as_the_api_expires_cancels_and_closes(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    with db.transaction(world.user) as conn:
        counts = guardian.cancel_open(conn, world.campaign, "campaign cancelled")
        assert stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="cancelled")
    assert counts["approvals_expired"] == 1
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=p.action_id) == "cancelled"
    assert scalar(engine, "SELECT stage::text FROM campaigns") == "closed"
    assert state_changes(engine) == [("Stage moved from researching to closed.", "cancelled")]


def test_the_state_change_function_refuses_a_campaign_that_is_not_the_callers(engine, clean):
    world = make_committed_world(engine)
    other = make_committed_world(engine, consent=False)
    with pytest.raises(LookupError), db.transaction(other.user) as conn:  # row-level security hides it from the API
        stages.pause(conn, world.campaign, "candidate")
    with pytest.raises(Exception, match="not your campaign"), db.transaction(other.user, role="worker") as conn:
        stages.pause(conn, world.campaign, "candidate")  # the worker sees it; the function refuses a non-owner
    assert scalar(engine, "SELECT count(*) FROM agent_actions WHERE kind = 'state_change'") == 0
    assert scalar(engine, "SELECT paused_at FROM campaigns WHERE id = :c", c=world.campaign) is None
