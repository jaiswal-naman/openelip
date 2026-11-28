"""Plan 07-03, GRD-02 and GRD-04: the follow-up leaves only after its undo window, to the typed address, after a fresh
pass of both keys; a late change of mind or block stops it."""

import email
from email import policy as email_policy

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import jobs  # noqa: F401  (registers follow_up_v1 with the relay)
from app.platform import db, relay
from app.trust import api as trust
from tests.guardian.support import (
    approve_and_release,
    entails,
    make_committed_world,
    make_due,
    propose_follow_up,
    relay_now,
    rows,
    scalar,
)
from tests.hiring.world import exclude_company


@pytest.fixture
def scheduled(engine, clean, smtp, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path, smtp=smtp)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    assert approve_and_release(world, p) == "scheduled"
    return world, p


def test_the_template_is_registered_with_the_relay():
    assert "follow_up_v1" in relay.RESOLVERS


def test_nothing_is_sent_before_the_window_ends_then_the_stored_message_goes_with_reply_to(engine, scheduled, smtp):
    world, p = scheduled
    assert relay_now() == 0 and smtp.messages == []  # the message is not due: the undo window is open
    make_due(engine, p.action_id)
    assert relay_now() == 1
    (sent,) = smtp.messages
    assert sent["to"] == "talent@lumen.example"
    mail = email.message_from_bytes(sent["data"], policy=email_policy.default)
    assert mail["Reply-To"] == "ada@example.test" and mail["To"] == "talent@lumen.example"
    assert mail["Subject"] == "Following up on my Data engineer application"
    assert "I applied on 3 November 2026." in mail.get_content()
    assert scalar(engine, "SELECT status::text FROM outbox") == "sent"
    # six new check rows were written at send time: proposal, release and send each ran both keys
    assert scalar(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :i", i=p.action_id) == 18


@pytest.mark.parametrize("change", ["cancelled", "employer_blocked", "consent_withdrawn", "dial_never", "paused"])
def test_a_change_between_release_and_send_blocks_the_action_and_nothing_is_sent(engine, scheduled, smtp, change):
    world, p = scheduled
    if change == "cancelled":
        with engine.begin() as c:
            c.execute(text("UPDATE campaigns SET stage = 'closed', closed_at = now(), closed_reason = 'cancelled'"))
        code = "campaign_closed"
    elif change == "employer_blocked":
        with engine.begin() as c:
            exclude_company(c, world.user, "Lumen Demo")
        code = "employer_blocked"
    elif change == "consent_withdrawn":
        with engine.begin() as c:
            trust.withdraw_consent(c, world.user, "ai_processing")
        code = "consent_missing"
    elif change == "dial_never":
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, 'send_follow_up', 'never')"
                ),
                {"u": world.user},
            )
        code = "dial_never"
    else:
        with engine.begin() as c:
            c.execute(text("UPDATE campaigns SET paused_at = now()"))
        code = "campaign_paused"
    make_due(engine, p.action_id)
    assert relay_now() == 0 and smtp.messages == []
    action = rows(engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=p.action_id)[0]
    assert tuple(action) == ("blocked", code)
    message = rows(engine, "SELECT status::text, last_error_code FROM outbox")[0]
    assert tuple(message) == ("cancelled", code)
    failed = rows(
        engine, "SELECT reason_code FROM guardian_checks WHERE agent_action_id = :i AND NOT passed", i=p.action_id
    )
    if change in (
        "employer_blocked",
        "consent_withdrawn",
    ):  # a check failed: its row says so (key one denials leave the policy reason)
        assert [f[0] for f in failed] == [code]
    else:
        assert failed == []


def test_a_held_message_is_not_claimed(engine, scheduled, smtp):
    world, p = scheduled
    with db.transaction(world.user) as conn:
        assert guardian.hold(conn, world.campaign) == 1
    make_due(engine, p.action_id)
    assert relay_now() == 0 and smtp.messages == []
    assert scalar(engine, "SELECT status::text FROM outbox") == "pending"


def test_an_action_that_is_no_longer_scheduled_is_refused_without_a_send(engine, scheduled, smtp):
    world, p = scheduled
    with engine.begin() as c:  # cancelled elsewhere while the row stayed pending
        c.execute(
            text("UPDATE agent_actions SET status = 'cancelled', outcome_code = 'undone' WHERE id = :i"),
            {"i": p.action_id},
        )
    make_due(engine, p.action_id)
    assert relay_now() == 0 and smtp.messages == []
    message = rows(engine, "SELECT status::text, last_error_code FROM outbox")[0]
    assert tuple(message) == ("cancelled", "action_not_scheduled")


def test_a_bad_payload_is_refused(engine, scheduled, smtp):
    world, p = scheduled
    with db.transaction(role="worker") as conn:
        assert relay.RESOLVERS["follow_up_v1"](conn, {}) == relay.Refusal("failed", "bad_payload")
