"""The outbox and its SMTP relay (ARCHITECTURE 5.5; invariant 6: nothing is sent twice).

The mail server is a small real SMTP server on a local port, so smtplib, the SMTP_URL parsing and the headers are
exercised for real; each test sets how it answers.
"""

import base64
import email
import socketserver
import threading
import time
from dataclasses import replace
from email import policy

import pytest
from sqlalchemy import text

from app.platform import auth, config, db, outbox, relay, sessions


class _Session(socketserver.StreamRequestHandler):
    def reply(self, code, message="ok"):
        self.wfile.write(f"{code} {message}\r\n".encode())
        self.wfile.flush()

    def handle(self):
        server, sender, recipient = self.server, None, None
        self.reply(220, "fake smtp")
        while line := self.rfile.readline():
            command = line.decode(errors="replace").strip()
            verb = command.upper()
            if verb.startswith("EHLO"):
                self.wfile.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
            elif verb.startswith("AUTH PLAIN"):
                server.logins.append(base64.b64decode(command.split()[2]).split(b"\0")[1:])
                self.reply(235, "welcome")
            elif verb.startswith("MAIL FROM"):
                sender = command.split("<")[1].split(">")[0]
                self.reply(250)
            elif verb.startswith("RCPT TO"):
                recipient = command.split("<")[1].split(">")[0]
                self.reply(*server.rcpt_reply)
            elif verb == "DATA":
                self.reply(354, "go on")
                data = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data += chunk
                time.sleep(server.data_delay)
                if server.data_reply[0] == 250:
                    server.messages.append({"from": sender, "to": recipient, "data": data})
                if server.drop_after_data:
                    return  # accepted, but the client never hears it
                self.reply(*server.data_reply)
            elif verb == "QUIT":
                self.reply(221, "bye")
                return
            else:
                self.reply(250)


class FakeSmtp(socketserver.ThreadingTCPServer):
    allow_reuse_address = daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Session)
        self.messages, self.logins = [], []
        self.rcpt_reply, self.data_reply = (250, "ok"), (250, "queued")
        self.drop_after_data, self.data_delay = False, 0.0

    def url(self, login=""):
        return f"smtp://{login}127.0.0.1:{self.server_address[1]}"


@pytest.fixture
def smtp(monkeypatch, api_client):  # api_client: empty tables before and after the test
    server = FakeSmtp()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = replace(config.get(), smtp_url=server.url(), mail_from="no-reply@careers.test")
    monkeypatch.setattr(config, "get", lambda: settings)
    auth._recent.clear()
    yield server
    server.shutdown()
    server.server_close()


def relay_now() -> int:
    with db.transaction(role="worker") as conn:
        return relay.relay_once(conn)


def queue_signin(client, engine, address="reader@mail.test"):
    assert client.post("/v1/auth/codes", json={"email": address}).status_code == 202
    with engine.connect() as c:
        return c.execute(text("SELECT id FROM outbox ORDER BY created_at DESC LIMIT 1")).scalar_one()


def row(engine, outbox_id):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM outbox WHERE id = :id"), {"id": outbox_id}).mappings().one()


def update(engine, sql, **params):
    with engine.begin() as c:
        c.execute(text(sql), params)


def test_adding_twice_with_one_dedupe_key_makes_one_row(engine, api_client):
    with db.transaction() as conn:
        first = outbox.add(conn, kind="signin", dedupe_key="signin:x", user_id=None, payload={})
        second = outbox.add(conn, kind="signin", dedupe_key="signin:x", user_id=None, payload={})
    assert first is not None and second is None
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 1


def test_a_due_signin_row_is_sent_once_with_the_code_and_the_sealed_code_is_blanked(engine, api_client, smtp):
    outbox_id = queue_signin(api_client, engine)
    sealed = row(engine, outbox_id)["payload"]["code_sealed"]
    assert relay_now() == 1 and relay_now() == 0  # the second call finds nothing to send
    (sent,) = smtp.messages
    mail = email.message_from_bytes(sent["data"], policy=policy.default)
    code = sessions.open_sealed(sealed)
    assert (sent["from"], sent["to"]) == ("no-reply@careers.test", "reader@mail.test")
    assert mail["Message-ID"] == f"<{outbox_id}@careers.test>" and mail["To"] == "reader@mail.test"
    assert config.get().product_name in mail["Subject"] and code in mail.get_content()
    done = row(engine, outbox_id)
    assert (
        done["status"] == "sent" and done["sent_at"] is not None and done["provider_message_id"] == mail["Message-ID"]
    )
    assert "code_sealed" not in done["payload"] and sealed not in str(dict(done))


def test_two_relays_at_once_send_it_once(engine, api_client, smtp):
    queue_signin(api_client, engine)
    smtp.data_delay = 0.5  # the first relay is still talking to the server when the second looks
    counts = []
    threads = [threading.Thread(target=lambda: counts.append(relay_now())) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(counts) == [0, 1] and len(smtp.messages) == 1


def test_a_row_not_yet_due_waits_and_an_expired_row_is_cancelled(engine, api_client, smtp):
    later, stale = queue_signin(api_client, engine, "a@mail.test"), queue_signin(api_client, engine, "b@mail.test")
    update(engine, "UPDATE outbox SET not_before = now() + interval '1 hour' WHERE id = :id", id=later)
    update(engine, "UPDATE outbox SET expires_at = now() - interval '1 second' WHERE id = :id", id=stale)
    assert relay_now() == 0 and not smtp.messages
    assert row(engine, later)["status"] == "pending"
    assert (row(engine, stale)["status"], row(engine, stale)["last_error_code"]) == ("cancelled", "expired")
    update(engine, "UPDATE outbox SET not_before = now() WHERE id = :id", id=later)
    assert relay_now() == 1


def test_a_temporary_smtp_error_goes_back_to_pending_with_a_delay_then_ends_after_the_last_try(
    engine, api_client, smtp
):
    outbox_id = queue_signin(api_client, engine)
    smtp.rcpt_reply = (451, "try again later")
    assert relay_now() == 0
    waiting = row(engine, outbox_id)
    assert (waiting["status"], waiting["attempts"], waiting["last_error_code"]) == ("pending", 1, "smtp_temporary")
    assert "code_sealed" in waiting["payload"]  # it is still needed for the retry
    assert relay_now() == 0 and row(engine, outbox_id)["attempts"] == 1  # not before its delay
    smtp.rcpt_reply = (250, "ok")
    update(engine, "UPDATE outbox SET not_before = now() WHERE id = :id", id=outbox_id)
    assert relay_now() == 1 and row(engine, outbox_id)["status"] == "sent"
    again = queue_signin(api_client, engine, "c@mail.test")
    smtp.rcpt_reply = (451, "still busy")
    update(engine, "UPDATE outbox SET attempts = :n WHERE id = :id", id=again, n=len(relay.RETRY_DELAYS))
    relay_now()
    assert (row(engine, again)["status"], row(engine, again)["last_error_code"]) == ("failed", "smtp_gave_up")


def test_a_permanent_rejection_fails_the_row(engine, api_client, smtp):
    outbox_id = queue_signin(api_client, engine)
    smtp.rcpt_reply = (550, "no such mailbox")
    assert relay_now() == 0 and relay_now() == 0
    failed = row(engine, outbox_id)
    assert (failed["status"], failed["last_error_code"], failed["attempts"]) == ("failed", "smtp_permanent", 1)
    assert "code_sealed" not in failed["payload"]


def test_a_send_with_an_unknown_outcome_is_never_retried_and_the_operator_settles_it(engine, api_client, smtp):
    outbox_id = queue_signin(api_client, engine)
    smtp.drop_after_data = True  # the server took the message, then the connection died before the answer
    assert relay_now() == 0
    assert row(engine, outbox_id)["status"] == "sending"
    assert relay_now() == 0 and row(engine, outbox_id)["status"] == "sending"  # its lease has not run out
    smtp.drop_after_data = False
    update(engine, "UPDATE outbox SET locked_at = now() - interval '10 minutes' WHERE id = :id", id=outbox_id)
    assert relay_now() == 0 and relay_now() == 0
    assert row(engine, outbox_id)["status"] == "unknown" and len(smtp.messages) == 1  # one copy, never a second
    relay.resolve(outbox_id, sent=True)
    assert (row(engine, outbox_id)["status"], row(engine, outbox_id)["sent_at"] is not None) == ("sent", True)
    with pytest.raises(SystemExit):  # it is no longer `unknown`
        relay.resolve(outbox_id, sent=False)


def test_a_crash_between_claim_and_send_leaves_unknown_and_resolve_failed_ends_it(engine, api_client, smtp):
    outbox_id = queue_signin(api_client, engine)
    update(
        engine,
        "UPDATE outbox SET status = 'sending', locked_at = now() - interval '6 minutes' WHERE id = :id",
        id=outbox_id,
    )
    assert relay_now() == 0 and not smtp.messages
    assert row(engine, outbox_id)["status"] == "unknown"
    relay.resolve(outbox_id, sent=False)
    assert (row(engine, outbox_id)["status"], row(engine, outbox_id)["last_error_code"]) == (
        "failed",
        "operator_not_sent",
    )


@pytest.mark.parametrize("address", ["tester@example.test", "tester@mail.example.test"])
def test_example_test_addresses_are_never_delivered(engine, api_client, smtp, address):
    outbox_id = queue_signin(api_client, engine, address)
    assert relay_now() == 0 and not smtp.messages
    assert (row(engine, outbox_id)["status"], row(engine, outbox_id)["last_error_code"]) == (
        "cancelled",
        "undeliverable",
    )


def test_a_synthetic_account_is_never_delivered_to(engine, api_client, smtp):
    update(engine, "INSERT INTO users (email, is_synthetic) VALUES ('demo@mail.test', true)")
    outbox_id = queue_signin(api_client, engine, "demo@mail.test")
    assert relay_now() == 0 and not smtp.messages and row(engine, outbox_id)["status"] == "cancelled"


def test_a_replaced_code_is_not_emailed(engine, api_client, smtp):
    first = queue_signin(api_client, engine)
    second = queue_signin(api_client, engine)
    assert relay_now() == 1 and len(smtp.messages) == 1
    assert (row(engine, first)["status"], row(engine, first)["last_error_code"]) == ("cancelled", "stale")
    assert row(engine, second)["status"] == "sent"


def test_an_unknown_template_fails_and_a_malformed_payload_fails_without_stopping_the_relay(engine, api_client, smtp):
    with db.transaction() as conn:
        outbox.add(conn, kind="mystery", dedupe_key="m:1", user_id=None, payload={})
        outbox.add(conn, kind="signin", dedupe_key="m:2", user_id=None, payload={"code_sealed": "garbage"})
    assert relay_now() == 0
    with engine.connect() as c:
        got = c.execute(text("SELECT dedupe_key, status, last_error_code FROM outbox ORDER BY dedupe_key")).all()
    assert [tuple(r) for r in got] == [("m:1", "failed", "unknown_template"), ("m:2", "failed", "bad_payload")]


def test_the_login_in_the_smtp_url_is_used(engine, api_client, smtp, monkeypatch):
    settings = replace(config.get(), smtp_url=smtp.url("robot%40mail.test:p%40ss@"))
    monkeypatch.setattr(config, "get", lambda: settings)
    queue_signin(api_client, engine)
    assert relay_now() == 1 and smtp.logins == [[b"robot@mail.test", b"p@ss"]]


def test_the_relay_logs_ids_and_outcomes_but_not_the_address_or_the_code(engine, api_client, smtp, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="app.event")
    outbox_id = queue_signin(api_client, engine, "private.person@mail.test")
    code = sessions.open_sealed(row(engine, outbox_id)["payload"]["code_sealed"])
    relay_now()
    assert "outbox.send" in caplog.text and str(outbox_id) in caplog.text
    assert "private" not in caplog.text and code not in caplog.text and "mail.test" not in caplog.text
