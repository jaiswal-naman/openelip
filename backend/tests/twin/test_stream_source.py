"""TWN-04, T-09-25 (plan 09-03): the Twin source of the live stream. Messages are read on the caller's API connection,
so row-level security decides who hears of what: the candidate hears of everything about them, an employer member of the
questions and Twin messages of their organization, never of a candidate's reply, and a member of another organization
hears of nothing. Everything is invented."""

import pytest

from app.platform import db, stream
from app.twin.stream import poll, snapshot
from tests.guardian.conftest import clean  # noqa: F401  (empties the tables around a committing test)
from tests.hiring.factories import make_member, make_org
from tests.twin.fixtures.graph import world as w


def start(user):
    with db.transaction(user) as conn:
        return snapshot(conn, user)


def next_poll(user, state):
    with db.transaction(user) as conn:
        return poll(conn, user, state)


@pytest.fixture
def world(engine, clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch, "unknown")
    return w.build(engine, dial="tell_me_after", topics={"experience": "answer"})


def names(events):
    return [(e.name, e.data["author"], e.data["answer_kind"]) for e in events]


def test_the_source_is_registered():
    assert "twin" in stream._sources


def test_the_snapshot_adds_nothing_and_old_messages_are_not_announced(engine, world):
    w.ask(world)
    part, state = start(world.candidate)
    assert part == {}
    assert next_poll(world.candidate, state)[0] == []


def test_the_candidate_hears_of_every_new_message_about_them_including_their_own_reply(engine, world):
    _, state = start(world.candidate)
    question = w.ask(world, "When could you start?")
    events, state = next_poll(world.candidate, state)
    assert names(events) == [("twin.message", "employer", None)]
    assert set(events[0].data) == {"session_id", "message_id", "author", "answer_kind"}
    w.run_jobs()  # classify, draft says unknown: the holding message
    events, state = next_poll(world.candidate, state)
    assert names(events) == [("twin.message", "twin", "unknown")]
    waiting = w.pending(engine, question)
    w.approve(world, waiting.approval_id, waiting.payload, reply="I can start in 30 days.")
    events, state = next_poll(world.candidate, state)
    assert names(events) == [("twin.message", "candidate", None)]
    w.run_jobs()
    events, state = next_poll(world.candidate, state)
    assert names(events) == [("twin.message", "twin", "relayed")]
    assert next_poll(world.candidate, state)[0] == []  # nothing is announced twice


def test_an_employer_member_hears_of_questions_and_twin_messages_but_never_a_reply(engine, world):
    _, state = start(world.member)
    question = w.ask(world, "When could you start?")
    w.run_jobs()
    waiting = w.pending(engine, question)
    w.approve(world, waiting.approval_id, waiting.payload, reply="I can start in 30 days.")
    events, state = next_poll(world.member, state)
    assert names(events) == [("twin.message", "employer", None), ("twin.message", "twin", "unknown")]
    w.run_jobs()
    events, _ = next_poll(world.member, state)
    assert names(events) == [("twin.message", "twin", "relayed")]


def test_a_member_of_another_organization_hears_of_nothing(engine, world):
    with engine.begin() as c:
        stranger = make_member(c, make_org(c, "Other Demo")).user
    _, state = start(stranger)
    w.ask(world)
    w.run_jobs()
    assert next_poll(stranger, state)[0] == []
