"""TRU-03, TWN-04 (plan 09-03): the Twin's entries in the candidate's audit log. One entry per Twin message about the
caller, built from the kind and the topic and never from a message body, with the organization and a link to /app/twin.
Everything is invented."""

from uuid import uuid4

import pytest

from app.platform import db
from app.trust import auditlog
from tests.guardian.conftest import clean  # noqa: F401  (empties the tables around a committing test)
from tests.twin.fixtures.graph import world as w


def read(user, **filters):
    with db.transaction(user) as conn:
        entries, cursor = auditlog.read(conn, user, auditlog.AuditFilter(limit=50, **filters))
    return [e for e in entries if e.source == "twin"], cursor


@pytest.fixture
def world(engine, clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch, "answerable")
    return w.build(engine, dial="tell_me_after", topics={"experience": "answer"})


def test_the_audit_log_shows_each_twin_message_about_the_candidate(engine, world, monkeypatch):
    answered = w.ask(world, "What did you build at your last job?")
    declined = w.ask(world, "What is your current salary?")
    w.run_jobs()
    entries, _ = read(world.candidate)
    by_kind = {e.kind: e for e in entries}
    assert sorted(by_kind) == ["twin.answered", "twin.declined"]
    answer = by_kind["twin.answered"]
    assert answer.summary == "Your Twin answered an employer's question about experience."
    assert (answer.actor_type, answer.outcome, answer.link) == (
        "agent",
        "answered",
        {"kind": "twin", "href": "/app/twin"},
    )
    assert answer.organization == {"id": str(world.world.org.id), "name": "Halden Demo"}
    assert by_kind["twin.declined"].summary == "Your Twin declined an employer's question about current pay."
    # a line carries the kind and the topic, never what was asked or answered
    for entry in entries:
        assert "salary" not in entry.summary and "pipelines" not in entry.summary
    assert answered and declined


def test_holding_messages_and_relays_appear_too(engine, world, monkeypatch):
    w.use_fakes(monkeypatch, "unknown")
    question = w.ask(world, "When could you start?")
    w.run_jobs()
    assert [e.kind for e in read(world.candidate)[0]] == ["twin.unknown"]
    waiting = w.pending(engine, question)
    w.approve(world, waiting.approval_id, waiting.payload, reply="I can start in 30 days.")
    w.run_jobs()
    entries, _ = read(world.candidate)
    assert sorted(e.kind for e in entries) == ["twin.relayed", "twin.unknown"]
    assert all("30 days" not in e.summary for e in entries)


def test_the_token_and_campaign_filters_return_no_twin_entries(engine, world):
    w.ask(world)
    w.run_jobs()
    assert read(world.candidate)[0]
    assert read(world.candidate, agent_token_id=uuid4())[0] == []
    assert read(world.candidate, campaign_id=uuid4())[0] == []


def test_nobody_else_reads_these_entries(engine, world):
    w.ask(world)
    w.run_jobs()
    assert read(world.member)[0] == []
    other = w.build(engine)
    assert read(other.candidate)[0] == []


def test_entries_are_cut_by_the_cursor_without_repeats(engine, world):
    for _ in range(3):
        w.ask(world, "What did you build at your last job?")
        w.run_jobs()
    with db.transaction(world.candidate) as conn:
        first, cursor = auditlog.read(conn, world.candidate, auditlog.AuditFilter(limit=2))
        assert cursor is not None
        rest, _ = auditlog.read(
            conn, world.candidate, auditlog.AuditFilter(limit=50, before=auditlog.decode_cursor(cursor))
        )
    twin_ids = [e.id for e in first + rest if e.source == "twin"]
    assert len(twin_ids) == len(set(twin_ids)) == 3
