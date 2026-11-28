"""Plan 08-01: the relay's capture sink and its one undeliverable check (INDEX decision 14).

Uses the fake SMTP server and helpers of test_relay.py. Every address is invented (example.test or mail.test).
"""

import pytest
from sqlalchemy import text

from app.platform import db, outbox, relay
from tests.platform import test_relay
from tests.platform.test_relay import queue_signin, relay_now, row, update

smtp = test_relay.smtp  # the fake SMTP server fixture, shared

TEMPLATE = "capture_test_v1"


@pytest.fixture
def custom(monkeypatch):
    """A resolver of the test's own, to prove the check covers templates the relay core never heard of."""

    def make(to: str, headers=None):
        monkeypatch.setitem(
            relay.RESOLVERS,
            TEMPLATE,
            lambda conn, payload: relay.Outgoing(to, "Hello", "Body text", headers or {"X-Test": "1"}),
        )
        with db.transaction() as conn:
            return outbox.add(conn, kind=TEMPLATE, dedupe_key=f"capture:{to}", user_id=None, payload={})

    return make


def capture_now(sink: list) -> int:
    with db.transaction(role="worker") as conn:
        return relay.relay_once(conn, capture=sink.append)


def test_a_normal_pass_refuses_example_test_and_synthetic_recipients_for_every_template(
    engine, api_client, smtp, custom
):
    update(engine, "INSERT INTO users (email, is_synthetic) VALUES ('demo@mail.test', true)")
    ids = [custom("someone@example.test"), custom("demo@mail.test"), custom("a@b.example.test")]
    assert relay_now() == 0 and not smtp.messages
    for outbox_id in ids:
        done = row(engine, outbox_id)
        assert (done["status"], done["last_error_code"]) == ("cancelled", "undeliverable")


def test_a_normal_pass_still_sends_to_a_real_looking_address(engine, api_client, smtp, custom):
    outbox_id = custom("reader@mail.test")
    assert relay_now() == 1
    assert [m["to"] for m in smtp.messages] == ["reader@mail.test"] and row(engine, outbox_id)["status"] == "sent"


def test_capture_hands_over_every_message_marks_it_sent_and_never_opens_smtp(engine, api_client, smtp, custom):
    outbox_id = custom("candidate@example.test")
    sink: list = []
    assert capture_now(sink) == 1
    (captured,) = sink
    assert isinstance(captured, relay.Captured)
    assert (captured.outbox_id, captured.template, captured.to) == (outbox_id, TEMPLATE, "candidate@example.test")
    assert (captured.subject, captured.body, captured.headers) == ("Hello", "Body text", {"X-Test": "1"})
    assert (captured.agent_action_id, captured.dedupe_key) == (None, "capture:candidate@example.test")
    done = row(engine, outbox_id)
    assert done["status"] == "sent" and done["provider_message_id"] == f"windtunnel:{outbox_id}"
    assert done["sent_at"] is not None
    assert smtp.messages == [] and smtp.logins == []
    assert capture_now([]) == 0  # settled once, never captured twice


def test_capture_never_opens_a_connection(engine, api_client, smtp, custom, monkeypatch):
    custom("candidate@example.test")

    def fail(*args, **kwargs):
        raise AssertionError("SMTP was opened during a capturing pass")

    monkeypatch.setattr(relay, "_send", fail)
    assert capture_now([]) == 1


def test_capture_sees_a_signin_message_too(engine, api_client, smtp):
    queue_signin(api_client, engine, "tester@example.test")
    sink: list = []
    assert capture_now(sink) == 1 and sink[0].template == "signin" and sink[0].to == "tester@example.test"


def test_a_refusal_is_settled_as_a_refusal_and_never_captured(engine, api_client, smtp):
    first = queue_signin(api_client, engine, "tester@example.test")
    second = queue_signin(api_client, engine, "tester@example.test")  # replaces the first code: it is now stale
    sink: list = []
    capture_now(sink)
    assert len(sink) == 1 and sink[0].outbox_id == second
    assert (row(engine, first)["status"], row(engine, first)["last_error_code"]) == ("cancelled", "stale")


def test_a_row_not_yet_due_is_not_claimed_by_a_capturing_pass(engine, api_client, smtp):
    outbox_id = queue_signin(api_client, engine, "tester@example.test")
    update(engine, "UPDATE outbox SET not_before = now() + interval '1 hour' WHERE id = :i", i=outbox_id)
    sink: list = []
    assert capture_now(sink) == 0 and sink == [] and row(engine, outbox_id)["status"] == "pending"


def test_the_row_ids_reach_the_sink_for_an_agent_message(engine, api_client, smtp, custom):
    outbox_id = custom("candidate@example.test")
    update(engine, "UPDATE outbox SET dedupe_key = 'follow_up:7' WHERE id = :i", i=outbox_id)  # the key is just text
    sink: list = []
    capture_now(sink)
    assert sink[0].dedupe_key == "follow_up:7"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM outbox WHERE status = 'sent'")).scalar_one() == 1
