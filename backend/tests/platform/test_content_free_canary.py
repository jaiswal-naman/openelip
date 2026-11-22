"""PLT-07: resume, chat, contact and query text never reach a log line, a metric or a span (ARCHITECTURE 9.2).

One scenario seeds unique canary strings as an email address, a resume paragraph, a chat message, a job-search query and
an exception message, and sends them through the places they could leak from: the API (sign-in, a search, a bad
request), the email relay, the model gateway (a valid answer and two invalid ones) and a queue job that raises. The test
then scans everything captured. The scan is itself tested: the same scenario with a deliberate leak must be reported,
so a test that went silent or blind would fail here rather than pass.
"""

import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, Counter, generate_latest
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url

from app.main import create_app
from app.platform import auth, config, db, log, queue, relay, telemetry
from app.platform.llm import SchemaInvalid, complete, fake

CONTACT = "canarycontactz7@mail.test"
RESUME = "canaryresumez7 led the ledger migration at Fabrikam"
CHAT = "canarychatz7 please find me a remote job"
QUERY = "canaryqueryz7"
EXCEPTION = "canaryboomz7 was pasted into an exception message"
TOKENS = {
    "contact": "canarycontactz7",
    "resume": "canaryresumez7",
    "chat": "canarychatz7",
    "query": "canaryqueryz7",
    "exception": "canaryboomz7",
}
DB_VARIABLES = ("DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER")
TABLES = "users, sessions, login_codes, idempotency_keys, outbox, queue_jobs, llm_calls, model_registry"


class Echo(BaseModel):
    answer: str


@queue.handler("canary.boom")
def _boom(conn: Any, payload: dict[str, Any]) -> None:
    raise RuntimeError(EXCEPTION)


@dataclass
class Captured:
    logs: str
    spans: str
    metrics: str


def leaks(captured: Captured) -> set[str]:
    """Which canary classes appear in which channel, in any letter case."""
    return {
        f"{name} in {channel}"
        for name, token in TOKENS.items()
        for channel, found in vars(captured).items()
        if token in found.lower()
    }


@dataclass
class World:
    client: TestClient
    engine: Engine
    spans: Any
    log_output: StringIO
    caplog: pytest.LogCaptureFixture
    mailbox: list[EmailMessage]
    tmp_path: Path

    def captured(self) -> Captured:
        every_record = "\n".join(record.getMessage() for record in self.caplog.records)
        return Captured(
            logs=self.log_output.getvalue() + "\n" + self.caplog.text + "\n" + every_record,
            spans="\n".join(s.to_json() for s in self.spans.get_finished_spans()),
            metrics=self.client.get("/metrics").text,
        )


def make_mailbox(sent: list[EmailMessage]) -> type:
    class Mailbox:
        """Stands in for smtplib.SMTP, so the relay really builds and hands over the message with no network."""

        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            pass

        def ehlo(self) -> None:
            pass

        def has_extn(self, name: str) -> bool:
            return False

        def send_message(self, message: EmailMessage) -> None:
            sent.append(message)

        def quit(self) -> None:
            pass

        def close(self) -> None:
            pass

    return Mailbox


@pytest.fixture(scope="module")
def canary_db(make_database: Any, alembic_run: Any) -> URL:
    url: URL = make_database(f"app_canary_{os.getpid()}_test")
    alembic_run("upgrade", url, "head")
    return url


@pytest.fixture
def world(
    canary_db: URL, spans: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[World]:
    admin = create_engine(canary_db.set(drivername="postgresql+psycopg"))
    with admin.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    urls = {
        name: make_url(os.environ[name]).set(database=canary_db.database).render_as_string(hide_password=False)
        for name in DB_VARIABLES
    }
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls})
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    auth._recent.clear()
    sent: list[EmailMessage] = []
    monkeypatch.setattr(relay.smtplib, "SMTP", make_mailbox(sent))
    # The test client's own request line carries the URL and its query: that is the client, not the application.
    # (Starlette's TestClient uses httpx2 and the lock also holds httpx; both log "HTTP Request: ..." at INFO.)
    client_names = ("httpx", "httpx2", "httpcore")
    client_loggers = [(logging.getLogger(name), logging.getLogger(name).level) for name in client_names]
    for client_logger, _ in client_loggers:
        client_logger.setLevel(logging.WARNING)
    caplog.set_level(logging.DEBUG)  # last: it also sets the threshold of the capture handler, so it must be DEBUG
    output = StringIO()
    previous = log._handler.setStream(output)  # what the JSON formatter writes to standard output, exactly
    with TestClient(create_app()) as client:
        yield World(client, admin, spans, output, caplog, sent, tmp_path)
    log._handler.setStream(previous)
    for client_logger, level in client_loggers:
        client_logger.setLevel(level)
    for application_engine in db._engines.values():
        application_engine.dispose()
    fake.replay_from(None)
    admin.dispose()


def emailed_code(mailbox: list[EmailMessage]) -> str:
    match = re.search(r"\b(\d{6})\b", mailbox[-1].get_content())
    assert match, "the relay's message holds the six-digit code"
    return match.group(1)


def run_scenario(w: World) -> Captured:
    client = w.client
    assert client.post("/v1/auth/codes", json={"email": CONTACT}).status_code == 202
    with db.transaction(role="worker") as conn:
        assert relay.relay_once(conn) == 1  # the message goes to the stub mail server
    assert w.mailbox[-1]["To"] == CONTACT  # the address and the code did travel; they just must not be logged
    code = emailed_code(w.mailbox)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    assert client.post("/v1/auth/sessions", json={"email": CONTACT, "code": wrong}).status_code == 401
    signed_in = client.post("/v1/auth/sessions", json={"email": CONTACT, "code": code, "timezone": "UTC"})
    assert signed_in.status_code == 201
    assert client.get("/v1/me").json()["data"]["email"] == CONTACT
    assert client.get("/v1/jobs", params={"q": QUERY}).status_code == 200
    assert client.get(f"/v1/{QUERY}/nowhere").status_code == 404
    assert client.post("/v1/auth/codes", json={"email": f"{RESUME} {CHAT}"}).status_code == 422

    recording = w.tmp_path / "recorded.json"
    recording.write_text(json.dumps([{"answer": f"{RESUME} {CHAT}"}, f"not json {RESUME}", f"still not json {CHAT}"]))
    fake.replay_from(recording)
    with w.engine.connect() as conn:
        user = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": CONTACT}).scalar_one()
    messages = [{"role": "user", "content": f"{RESUME}\n{CHAT}"}]
    answer = complete("canary.probe", Echo, messages, principal=user, prompt_version="canary-v1")
    assert isinstance(answer, Echo) and RESUME in answer.answer  # the text flowed through the gateway
    with pytest.raises(SchemaInvalid) as refused:
        complete("canary.probe", Echo, messages, principal=user, prompt_version="canary-v1")
    assert not leaks(Captured(str(refused.value), "", ""))  # the exception's own message is clean

    with db.transaction(role="worker") as conn:
        queue.enqueue(conn, "canary.boom", {"note": RESUME}, unique_key="canary-boom")
    assert queue.run_once(lambda: db.transaction(role="worker"), "canary-worker") == 1
    return w.captured()


def test_no_canary_reaches_a_log_line_a_metric_or_a_span(world):
    logging.getLogger("canary.probe").debug("a debug-level probe")
    captured = run_scenario(world)
    assert leaks(captured) == set()
    # Silence would pass the check above, so prove the captures hold what the scenario should have produced.
    assert "a debug-level probe" in captured.logs  # records of every level from every logger are captured
    events = {json.loads(line)["event"] for line in world.log_output.getvalue().splitlines() if line.startswith("{")}
    assert {"auth.code_requested", "auth.failed", "auth.signed_in", "outbox.send", "llm.call", "queue.job"} <= events
    assert {s.name for s in world.spans.get_finished_spans()} >= {"http.request", "queue.job", "llm.complete"}
    assert 'route="/v1/jobs"' in captured.metrics and 'route="unmatched"' in captured.metrics
    assert 'operations_total{operation="queue.job",outcome="error"}' in captured.metrics
    assert 'operations_total{operation="llm.complete",outcome="error"}' in captured.metrics


def test_the_scan_reports_a_canary_written_to_the_log_around_log_event(world):
    logging.getLogger("app.event").warning("resume text: %s", RESUME)
    assert leaks(world.captured()) == {"resume in logs"}


def test_the_scan_reports_a_canary_an_allow_listed_key_lets_through(world, monkeypatch):
    # The allow-list is by key, so text under an allowed key is the one hole the canary test exists to catch.
    real = log.event
    monkeypatch.setattr(log, "event", lambda name, /, **fields: real(name, **{**fields, "kind": CONTACT}))
    assert "contact in logs" in leaks(run_scenario(world))


def test_the_scan_reports_a_canary_an_allow_listed_span_attribute_lets_through(world, monkeypatch):
    real = telemetry.span
    monkeypatch.setattr(telemetry, "span", lambda name, **attributes: real(name, **{**attributes, "route": CHAT}))
    assert "chat in spans" in leaks(run_scenario(world))


def test_the_scan_reports_the_exception_message_the_library_defaults_would_copy_into_a_span(world):
    with pytest.raises(RuntimeError), telemetry._provider.get_tracer("raw").start_as_current_span("raw"):
        raise RuntimeError(EXCEPTION)  # record_exception defaults on: the span keeps str(exc), which is what we avoid
    assert leaks(world.captured()) == {"exception in spans"}


def test_the_scan_reports_a_canary_in_a_span_attribute_and_in_a_metric_label(world):
    with telemetry._provider.get_tracer("raw").start_as_current_span("raw", attributes={"note": CHAT}):
        pass
    private = CollectorRegistry()  # not the shared registry: a leaked label there would stay for the whole session
    Counter("leaked", "A counter labelled with text.", ["text"], registry=private).labels(QUERY).inc()
    captured = world.captured()
    captured.metrics = generate_latest(private).decode()
    assert leaks(captured) == {"chat in spans", "query in metrics"}
