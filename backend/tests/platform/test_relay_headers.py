"""The relay's optional message headers (plan 04-05, RFC 8058): a resolver may add headers to its message, and the
sign-in email, which adds none, is unchanged. The mail server is the small real SMTP server of test_relay.py."""

import email
import threading
from dataclasses import replace
from email import policy

import pytest

from app.platform import config, db, outbox, relay
from tests.platform.test_relay import FakeSmtp, queue_signin, relay_now

UNSUBSCRIBE = {
    "List-Unsubscribe": "<https://app.test/v1/briefs/unsubscribe?t=abc>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
}


def outgoing(**fields):
    return relay.Outgoing("reader@mail.test", "Hello", "Body.\n", **fields)


def test_headers_are_set_on_the_message_and_a_plain_message_has_none_of_them():
    settings = config.get()

    with_headers = relay._message(settings, "<id@host>", outgoing(headers=UNSUBSCRIBE))
    plain = relay._message(settings, "<id@host>", outgoing())

    assert with_headers["List-Unsubscribe"] == UNSUBSCRIBE["List-Unsubscribe"]
    assert with_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert with_headers["To"] == "reader@mail.test" and with_headers["Message-ID"] == "<id@host>"
    assert plain["List-Unsubscribe"] is None and plain["List-Unsubscribe-Post"] is None


def test_a_header_value_with_a_line_break_is_refused_so_nothing_can_be_injected():
    """T-04-26: headers go through the email library, which will not take a value that could start another header."""
    settings = config.get()
    hostile = {"List-Unsubscribe": "<https://app.test/x>\r\nBcc: someone@mail.test"}

    with pytest.raises(ValueError):
        relay._message(settings, "<id@host>", outgoing(headers=hostile))


@pytest.fixture
def smtp(monkeypatch, api_client):  # api_client: empty tables before and after the test
    server = FakeSmtp()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = replace(config.get(), smtp_url=server.url(), mail_from="no-reply@careers.test")
    monkeypatch.setattr(config, "get", lambda: settings)
    yield server
    server.shutdown()
    server.server_close()


def test_a_resolvers_headers_reach_the_sent_message_and_the_signin_email_carries_none(
    engine, api_client, smtp, monkeypatch
):
    monkeypatch.setitem(
        relay.RESOLVERS, "headers_probe", lambda conn, payload: outgoing(headers=UNSUBSCRIBE)
    )  # a template of this test's own
    queue_signin(api_client, engine, "signer@mail.test")
    with db.transaction() as conn:
        outbox.add(conn, kind="headers_probe", dedupe_key="probe:1", user_id=None, payload={})

    assert relay_now() == 2

    sent = {}
    for raw in smtp.messages:
        message = email.message_from_bytes(raw["data"], policy=policy.default)
        sent[message["To"]] = message
    probe, signin = sent["reader@mail.test"], sent["signer@mail.test"]
    assert probe["List-Unsubscribe"] == UNSUBSCRIBE["List-Unsubscribe"]
    assert probe["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert signin["List-Unsubscribe"] is None and signin["List-Unsubscribe-Post"] is None
    assert "sign-in code" in signin["Subject"]


def test_the_undeliverable_rule_is_public_for_every_resolver_to_ask(engine, api_client):
    with db.transaction(role="worker") as conn:
        assert relay.is_undeliverable(conn, "someone@example.test") is True
        assert relay.is_undeliverable(conn, "someone@mail.example.test") is True
        assert relay.is_undeliverable(conn, "someone@mail.test") is False
