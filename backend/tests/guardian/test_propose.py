"""Plan 07-03, GRD-02: every proposed outside action gets a policy decision and six check results before anyone sees
it, and
a denied or failed one is blocked and logged with its reason."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import checks, policy
from app.platform import db, events
from tests.guardian.support import (
    entails,
    follow_up_payload,
    follow_up_provenance,
    make_committed_world,
    propose_follow_up,
    rows,
    scalar,
)

KINDS = ["citations", "consent", "scope", "budget", "injection", "rate_limit"]


@pytest.fixture
def subscribed(monkeypatch):
    monkeypatch.setattr(events, "_subscribers", {"guardian.action_changed": [("campaign.resume", "default")]})


def test_a_proposal_is_recorded_with_its_policy_decision_six_checks_and_an_approval(
    engine, clean, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    proposed = propose_follow_up(world)
    assert (proposed.status, proposed.failed_codes) == ("awaiting_approval", ())
    action = rows(
        engine,
        "SELECT status::text, policy_decision::text, policy_version, kind::text, payload ->> 'to' AS to_address, "
        "inputs ->> 'step_id' AS step FROM agent_actions WHERE id = :i",
        i=proposed.action_id,
    )[0]
    assert tuple(action) == (
        "awaiting_approval",
        "ask",
        policy.POLICY_VERSION,
        "outside_action",
        "talent@lumen.example",
        "follow_up:1",
    )
    checked = rows(
        engine,
        "SELECT check_kind::text, passed, guardian_version FROM guardian_checks WHERE agent_action_id = :i ORDER BY id",
        i=proposed.action_id,
    )
    assert [c[0] for c in checked] == KINDS and all(c[1] for c in checked)
    assert {c[2] for c in checked} == {checks.GUARDIAN_VERSION}
    days = scalar(
        engine,
        "SELECT extract(epoch FROM expires_at - requested_at) / 86400 FROM approvals WHERE id = :a",
        a=proposed.approval_id,
    )
    assert round(float(days), 3) == 7.0


def test_proposing_again_for_the_same_step_returns_the_action_already_made(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    first = propose_follow_up(world)
    with db.transaction(world.user, role="worker") as conn:
        run = conn.execute(text("SELECT id FROM agent_runs WHERE campaign_id = :c"), {"c": world.campaign}).scalar_one()
        again = guardian.propose_action(
            conn,
            run,
            action_type="send_follow_up",
            payload=follow_up_payload(),
            provenance=follow_up_provenance(),
            inputs={"step_id": "follow_up:1"},
        )
    assert again.action_id == first.action_id and again.approval_id == first.approval_id
    assert scalar(engine, "SELECT count(*) FROM agent_actions WHERE kind = 'outside_action'") == 1
    assert scalar(engine, "SELECT count(*) FROM guardian_checks") == 6


def test_a_proposal_publishes_the_action_changed_event_for_the_campaign_thread(
    engine, clean, subscribed, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    proposed = propose_follow_up(world)
    keys = [r[0] for r in rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume'")]
    assert keys == [f"campaign.resume:{world.campaign}:a{proposed.action_id}.awaiting_approval"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("paused", "campaign_paused"),
        ("closed", "campaign_closed"),
        ("never", "dial_never"),
        ("inactive", "account_inactive"),
    ],
)
def test_the_policy_engine_blocks_and_every_check_still_leaves_its_row(
    engine, clean, monkeypatch, tmp_path, setup, code
):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    with engine.begin() as c:
        sql = {
            "paused": "UPDATE campaigns SET paused_at = now() WHERE id = :c",
            "closed": "UPDATE campaigns SET stage = 'closed', closed_at = now(), "
            "closed_reason = 'cancelled' WHERE id = :c",
            "never": "INSERT INTO autonomy_settings (user_id, action_type, level) "
            "SELECT user_id, 'send_follow_up', 'never' FROM campaigns WHERE id = :c",
            "inactive": "UPDATE users SET status = 'suspended' "
            "WHERE id = (SELECT user_id FROM campaigns WHERE id = :c)",
        }[setup]
        c.execute(text(sql), {"c": world.campaign})
    proposed = propose_follow_up(world)
    assert proposed.status == "blocked" and proposed.approval_id is None
    row = rows(
        engine,
        "SELECT status::text, outcome_code, policy_decision::text FROM agent_actions WHERE id = :i",
        i=proposed.action_id,
    )[0]
    assert tuple(row) == ("blocked", code, "deny")
    assert scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=proposed.action_id) == 6
    assert scalar(engine, "SELECT count(*) FROM approvals") == 0


def test_a_failed_check_blocks_the_action_with_its_reason(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    proposed = propose_follow_up(world, payload=follow_up_payload(to="someone.else@lumen.example"))
    assert (proposed.status, proposed.failed_codes) == ("blocked", ("restricted_data",))
    outcome = scalar(engine, "SELECT outcome_code FROM agent_actions WHERE id = :i", i=proposed.action_id)
    assert outcome == "restricted_data"
    failed = rows(
        engine,
        "SELECT check_kind::text, reason_code FROM guardian_checks WHERE agent_action_id = :i AND NOT passed",
        i=proposed.action_id,
    )
    assert [tuple(f) for f in failed] == [("scope", "restricted_data")]


def test_the_twin_answer_type_is_blocked_until_its_checks_exist(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "twin", "test-1")
        proposed = guardian.propose_action(
            conn,
            run,
            action_type="twin_answer",
            payload={"answer": "Yes."},
            provenance={"fields": {"answer": "candidate"}},
        )
    assert proposed.status == "blocked"
    assert (
        scalar(engine, "SELECT outcome_code FROM agent_actions WHERE id = :i", i=proposed.action_id)
        == "type_not_available"
    )


def test_with_no_citation_check_registered_the_draft_fails_closed(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    checks.register_citation_check(None)
    proposed = propose_follow_up(world)
    assert (proposed.status, proposed.failed_codes) == ("blocked", ("citation_check_missing",))
