"""08-02: the hiring source of the live stream (`introduction.status`). Invented rows from tests/hiring/factories.py,
committed, read through the source on the caller's API connection. The events come from the list views the introduction
routes use, so an employer member sees what their list shows and nothing of a candidate's private decline."""

import pytest
from sqlalchemy import text

from app.hiring import answers
from app.hiring.stream import poll, snapshot
from app.platform import db
from tests.conftest import _empty_tables
from tests.hiring.factories import make_accepted, make_ask, make_candidate, make_member, make_open_role, make_org
from tests.hiring.test_introduction_routes import scene


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def start(user):
    with db.transaction(user) as conn:
        return snapshot(conn, user)


def next_poll(user, state):
    with db.transaction(user) as conn:
        return poll(conn, user, state)


def test_a_candidate_is_told_when_their_introduction_is_accepted_and_when_it_is_answered(clean):
    s = scene(clean)  # an asked introduction
    part, state = start(s.candidate.id)
    assert part == {} and next_poll(s.candidate.id, state)[0] == []
    with clean.begin() as conn:
        make_accepted(conn, s.role, s.candidate, ask=s.ask)
    events, state = next_poll(s.candidate.id, state)
    assert [(e.name, e.data["introduction_id"], e.data["status"], e.data["employer_answer"]) for e in events] == [
        ("introduction.status", s.ask.id, "accepted", None)
    ]
    assert e_id_none(events)
    with db.transaction(s.member) as conn:
        answers.answer(conn, s.member, s.ask.id, "yes", "Strong fit")
    events, state = next_poll(s.candidate.id, state)
    assert [(e.data["status"], e.data["employer_answer"]) for e in events] == [("accepted", "yes")]
    assert next_poll(s.candidate.id, state)[0] == []


def e_id_none(events):
    return all(e.id is None for e in events)


def test_a_new_introduction_after_the_snapshot_is_an_event(clean):
    s = scene(clean)
    _, state = start(s.candidate.id)
    with clean.begin() as conn:
        second = make_ask(conn, make_open_role(conn, s.org, title="Analyst"), s.candidate)
    events, _ = next_poll(s.candidate.id, state)
    assert [(e.data["introduction_id"], e.data["status"]) for e in events] == [(second.id, "asked")]


def test_an_employer_member_sees_what_their_list_shows_and_no_private_decline(clean):
    s = scene(clean, accepted=True)
    with clean.begin() as conn:
        declined_candidate = make_candidate(conn, name="Demo Decliner")
        declined = make_ask(conn, s.role, declined_candidate)
        asked_only = make_ask(conn, s.role, make_candidate(conn, name="Demo Waiting"))
    _, state = start(s.member)
    assert s.ask.id in state and declined.id not in state and asked_only.id not in state
    with db.transaction(declined_candidate.id) as conn:
        conn.execute(text("UPDATE introductions SET status = 'declined' WHERE id = :i"), {"i": declined.id})
    assert next_poll(s.member, state)[0] == []  # a decline stays between the candidate and us
    with db.transaction(s.member) as conn:
        answers.answer(conn, s.member, s.ask.id, "no", "Not this time")
    events, _ = next_poll(s.member, state)
    (event,) = events
    assert event.data["introduction_id"] == s.ask.id and event.data["employer_answer"] == "no"
    assert event.data["answer_due_at"] is not None
    assert "reason" not in str(event.data) and "Not this time" not in str(event.data)


def test_a_second_person_and_a_foreign_organization_get_nothing(clean):
    s = scene(clean, accepted=True)
    with clean.begin() as conn:
        outsider = make_candidate(conn, name="Demo Outsider")
        other_org = make_org(conn, "Other Demo")
        stranger = make_member(conn, other_org).user
    for person in (outsider.id, stranger, other_org.owner):
        _, state = start(person)
        assert state == {}
    _, outsider_state = start(outsider.id)
    _, stranger_state = start(stranger)
    with db.transaction(s.member) as conn:
        answers.answer(conn, s.member, s.ask.id, "yes", "Good")
    assert next_poll(outsider.id, outsider_state)[0] == []
    assert next_poll(stranger, stranger_state)[0] == []
