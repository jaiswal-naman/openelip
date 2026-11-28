"""08-02: the campaigns source of the live stream. Rows are invented (tests/campaigns/factories.py), committed, and read
through the source's own snapshot and poll on a connection that acts for the caller (the API login, row-level security
on). Changes between polls go through the modules' own functions."""

import json

import pytest
from sqlalchemy import text

from app.campaigns import stages
from app.campaigns.stream import poll, snapshot
from app.platform import db
from tests.campaigns.factories import make_campaign, make_step, make_world
from tests.conftest import _empty_tables
from tests.guardian.support import World, add_spend
from tests.match.factories import make_job, make_user


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def world_of(engine, **kwargs):
    with engine.begin() as conn:
        w = make_world(conn, **kwargs)
    return World(w.user, w.job.id, w.campaign)


def start(user):
    with db.transaction(user) as conn:
        return snapshot(conn, user)


def next_poll(user, state):
    with db.transaction(user) as conn:
        return poll(conn, user, state)


def names(events):
    return [e.name for e in events]


def test_snapshot_lists_open_campaigns_with_steps_spend_and_budget(clean):
    w = world_of(clean)
    with clean.begin() as conn:
        make_step(conn, w.campaign, "research", status="running")
    add_spend(clean, w, 12_000)
    part, _ = start(w.user)
    assert part["campaigns"] == [
        {
            "id": w.campaign,
            "stage": "researching",
            "paused_at": None,
            "pause_reason": None,
            "spent_usd": "0.012000",
            "budget_usd": "0.750000",
        }
    ]
    (step,) = part["steps"]
    assert (step["campaign_id"], step["step_key"], step["agent_role"], step["status"]) == (
        w.campaign,
        "research",
        "researcher",
        "running",
    )
    assert step["waiting_for_approval_id"] is None


def test_a_closed_campaign_is_not_in_the_snapshot(clean):
    w = world_of(clean, stage="closed")
    part, _ = start(w.user)
    assert part == {"campaigns": [], "steps": []}


def test_a_stage_move_a_pause_and_a_resume_are_events(clean):
    w = world_of(clean)
    _, state = start(w.user)
    with db.transaction(w.user) as conn:
        assert stages.move(conn, w.campaign, "needs_you", cause="test", wake=False)
    events, state = next_poll(w.user, state)
    assert [(e.name, e.data["from"], e.data["to"], e.id) for e in events] == [
        ("campaign.stage", "researching", "needs_you", None)
    ]
    assert next_poll(w.user, state)[0] == []  # nothing changed: nothing sent
    with db.transaction(w.user) as conn:
        assert stages.pause(conn, w.campaign, "budget_exhausted", wake=False)
    events, state = next_poll(w.user, state)
    (paused,) = events
    assert paused.data["to"] == "needs_you" and paused.data["pause_reason"] == "budget_exhausted"
    assert paused.data["paused_at"] is not None
    with db.transaction(w.user) as conn:
        assert stages.unpause(conn, w.campaign, by="candidate", wake=False)
    events, _ = next_poll(w.user, state)
    assert events[0].data["paused_at"] is None and events[0].data["pause_reason"] is None


def test_a_campaign_that_closes_during_the_stream_is_reported_once(clean):
    w = world_of(clean)
    _, state = start(w.user)
    with db.transaction(w.user) as conn:
        stages.move(conn, w.campaign, "closed", cause="test", closed_reason="cancelled", wake=False)
    events, state = next_poll(w.user, state)
    assert [(e.name, e.data["to"], e.data["closed_reason"]) for e in events] == [
        ("campaign.stage", "closed", "cancelled")
    ]
    assert next_poll(w.user, state)[0] == []


def test_a_campaign_created_after_the_snapshot_has_a_null_from(clean):
    w = world_of(clean)
    _, state = start(w.user)
    with clean.begin() as conn:
        job = make_job(conn, title="Analyst", description="Analyse data with SQL.", requirements=[("SQL", "required")])
        second = make_campaign(conn, w.user, job.id)
        make_step(conn, second, "research")
    events, _ = next_poll(w.user, state)
    assert names(events) == ["campaign.stage", "step.status"]
    assert events[0].data["from"] is None and events[0].data["campaign_id"] == second
    assert events[1].data["status"] == "pending"


def test_step_changes_are_events_and_unchanged_steps_are_not(clean):
    w = world_of(clean)
    with clean.begin() as conn:
        make_step(conn, w.campaign, "research")
    _, state = start(w.user)
    assert next_poll(w.user, state)[0] == []
    with clean.begin() as conn:
        conn.execute(text("UPDATE campaign_steps SET status = 'running', attempts = 1 WHERE step_key = 'research'"))
    events, state = next_poll(w.user, state)
    assert [(e.name, e.data["step_key"], e.data["status"]) for e in events] == [("step.status", "research", "running")]
    assert next_poll(w.user, state)[0] == []


def test_spend_changes_make_cost_updated_and_nothing_else(clean):
    w = world_of(clean)
    _, state = start(w.user)
    add_spend(clean, w, 6_100)
    events, state = next_poll(w.user, state)
    assert [(e.name, e.data) for e in events] == [
        ("cost.updated", {"campaign_id": w.campaign, "spent_usd": "0.006100", "budget_usd": "0.750000"})
    ]
    assert next_poll(w.user, state)[0] == []


def test_another_persons_campaigns_never_appear(clean):
    mine = world_of(clean)
    theirs = world_of(clean)
    with clean.begin() as conn:
        make_step(conn, theirs.campaign, "research")
    add_spend(clean, theirs, 5_000)
    part, state = start(mine.user)
    assert [c["id"] for c in part["campaigns"]] == [mine.campaign] and part["steps"] == []
    with db.transaction(theirs.user) as conn:
        stages.move(conn, theirs.campaign, "needs_you", cause="test", wake=False)
    add_spend(clean, theirs, 5_000)
    assert next_poll(mine.user, state)[0] == []
    other, _ = start(theirs.user)
    assert [c["id"] for c in other["campaigns"]] == [theirs.campaign]


def test_a_person_with_no_campaigns_gets_empty_lists(clean):
    with clean.begin() as conn:
        user = make_user(conn)
    assert start(user)[0] == {"campaigns": [], "steps": []}


def test_the_real_app_streams_my_snapshot_and_nothing_of_anyone_elses(clean, api_client):
    """The whole path on app.main.create_app(): sign in, open the stream, read the snapshot and one poll."""
    from app.platform import sessions, stream
    from app.platform.stream import Limits

    mine, theirs = world_of(clean), world_of(clean)
    with clean.begin() as conn:
        make_step(conn, mine.campaign, "research", status="running")
        make_step(conn, theirs.campaign, "research", status="running")
    with db.transaction(mine.user) as conn:
        token = sessions.start(conn, mine.user)
    api_client.cookies.set(sessions.COOKIE_NAME, token)
    api_client.app.dependency_overrides[stream.limits] = lambda: Limits(2, 0, 3600)
    with db.transaction(theirs.user) as conn:
        stages.move(conn, theirs.campaign, "needs_you", cause="test", wake=False)
    with api_client.stream("GET", "/v1/control/stream") as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: error" not in body
    snapshot_line = next(line for line in body.splitlines() if line.startswith("data: "))
    data = json.loads(snapshot_line[6:])
    assert set(data) == {"campaigns", "steps", "pending_approvals"}
    assert [c["id"] for c in data["campaigns"]] == [str(mine.campaign)]
    assert [s["campaign_id"] for s in data["steps"]] == [str(mine.campaign)]
    assert str(theirs.campaign) not in body and str(theirs.user) not in body
