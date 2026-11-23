"""HIR-08: serious-interest signals with the weekly quota (plan 06-04)."""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.hiring import signals
from app.platform import db
from app.platform.errors import ApiError
from tests.hiring import world
from tests.hiring.factories import make_ask, make_open_role
from tests.hiring.test_introduction_routes import act, another_ask, key, make_stranger, rows, scene


def send(client, intro, headers=None):
    return client.post("/v1/signals", json={"introduction_id": str(intro)}, headers=headers or key())


def next_monday() -> datetime:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=7)


def test_a_candidate_signal_on_an_ask_is_stored_for_the_week_and_not_visible_to_the_employer(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)

    response = send(api_client, s.ask.id)

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["signal"]["sender_side"] == "candidate" and data["signal"]["role_id"] == str(s.role.id)
    assert data["signal"]["is_visible_to_employer"] is False
    assert data["quota"]["used"] == 1 and data["quota"]["limit"] == 3
    assert datetime.fromisoformat(data["quota"]["resets_at"]) == next_monday()
    stored = rows(
        engine,
        "SELECT sender_side::text, introduction_id, slot, sent_by, week_start = date_trunc('week', now())::date "
        "FROM interest_signals",
    )
    assert [tuple(r) for r in stored] == [("candidate", None, 1, s.candidate.id, True)]
    assert rows(engine, "SELECT count(*) FROM audit_log WHERE event_type = 'hiring.signal_sent'") == [(1,)]


def test_after_acceptance_the_candidates_signal_is_visible_and_the_employer_can_answer_in_kind(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)
    assert send(api_client, s.ask.id).json()["data"]["signal"]["is_visible_to_employer"] is True
    act(api_client, s.member)
    response = send(api_client, s.ask.id)
    assert response.status_code == 201 and response.json()["data"]["signal"]["sender_side"] == "organization"
    stored = rows(
        engine, "SELECT sender_side::text, introduction_id, sent_by, user_id FROM interest_signals ORDER BY created_at"
    )
    assert [tuple(r) for r in stored] == [
        ("candidate", None, s.candidate.id, s.candidate.id),
        ("organization", s.ask.id, s.member, s.candidate.id),
    ]
    assert rows(
        engine,
        "SELECT subject_user_id, actor_type::text FROM audit_log WHERE event_type = 'hiring.signal_sent' ORDER BY id",
    )[1] == (None, "employer")


def test_an_employer_cannot_signal_an_asked_candidate_in_the_api_or_the_database(engine, api_client):
    s = scene(engine)
    act(api_client, s.member)
    assert send(api_client, s.ask.id).status_code == 404  # the employer cannot even see it
    assert rows(engine, "SELECT count(*) FROM interest_signals") == [(0,)]
    with engine.connect() as c, pytest.raises(DBAPIError) as refused:  # G7: the database refuses it as well
        c.execute(text("SET LOCAL ROLE app_api"))
        c.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(s.member)})
        c.execute(
            text(
                """INSERT INTO interest_signals
                     (sender_side, user_id, organization_id, role_id, introduction_id, sent_by, week_start, slot)
                   VALUES ('organization', :u, :o, :r, :i, :m,
                           date_trunc('week', now() AT TIME ZONE 'UTC')::date, 1)"""
            ),
            {"u": s.candidate.id, "o": s.org.id, "r": s.role.id, "i": s.ask.id, "m": s.member},
        )
    assert "organization signal must ride" in world.message(refused)


def test_a_second_signal_for_the_same_role_is_already_resolved(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    send(api_client, s.ask.id)
    again = send(api_client, s.ask.id)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_resolved"
    assert rows(engine, "SELECT count(*) FROM interest_signals") == [(1,)]


def roles_with_asks(engine, s, n):
    with engine.begin() as c:
        return [make_ask(c, make_open_role(c, s.org, title=f"Role {i}"), s.candidate) for i in range(n)]


def test_a_fourth_signal_in_a_week_is_quota_exhausted_with_the_reset_time(engine, api_client):
    s = scene(engine)
    asks = [s.ask, *roles_with_asks(engine, s, 3)]
    act(api_client, s.candidate.id)
    assert [send(api_client, a.id).status_code for a in asks[:3]] == [201, 201, 201]

    refused = send(api_client, asks[3].id)

    assert refused.status_code == 422 and refused.json()["error"]["code"] == "quota_exhausted"
    assert datetime.fromisoformat(refused.json()["error"]["details"]["resets_at"]) == next_monday()
    assert rows(engine, "SELECT count(*) FROM interest_signals") == [(3,)]


def test_the_quota_is_back_in_the_next_week(engine, api_client):
    s = scene(engine)
    asks = [s.ask, *roles_with_asks(engine, s, 3)]
    with engine.begin() as c:  # three signals sent last week
        last_week = datetime.now(timezone.utc) - timedelta(days=7)
        for slot, ask in enumerate(asks[:3], start=1):
            world.signal(
                c,
                "candidate",
                s.candidate.id,
                s.org.id,
                ask.role,
                sent_by=s.candidate.id,
                slot=slot,
                created_at=last_week,
            )
    act(api_client, s.candidate.id)
    response = send(api_client, asks[3].id)
    assert response.status_code == 201 and response.json()["data"]["quota"]["used"] == 1


def test_the_organization_has_its_own_quota(engine, api_client):
    s = scene(engine, accepted=True)
    others = []
    for _ in range(3):
        _, ask = another_ask(engine, s, accepted=True)
        others.append(ask)
    act(api_client, s.member)
    assert [send(api_client, a.id).status_code for a in [s.ask, *others[:2]]] == [201, 201, 201]
    refused = send(api_client, others[2].id)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "quota_exhausted"


def test_two_sends_racing_for_the_last_slot_let_exactly_one_through(engine, api_client):
    s = scene(engine)
    asks = [s.ask, *roles_with_asks(engine, s, 3)]
    act(api_client, s.candidate.id)
    send(api_client, asks[0].id)
    send(api_client, asks[1].id)  # two used: one slot left, two sends (for two roles) want it
    outcomes: list[str] = []
    gate = threading.Barrier(2)

    def attempt(intro):
        try:
            with db.transaction(s.candidate.id) as conn:
                assert signals._quota(conn, "candidate", s.candidate.id, s.org.id)["used"] == 2  # both see two used
                gate.wait(timeout=10)
                outcomes.append(signals.send(conn, s.candidate.id, intro)["signal"]["sender_side"])
        except ApiError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(asks[i].id,)) for i in (2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(outcomes) == ["candidate", "quota_exhausted"], outcomes
    assert rows(engine, "SELECT count(*), max(slot) FROM interest_signals") == [(3, 3)]


def test_a_closed_introduction_cannot_be_signalled(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    api_client.post(f"/v1/introductions/{s.ask.id}/decline", headers=key())
    refused = send(api_client, s.ask.id)
    assert (refused.status_code, refused.json()["error"]["details"]) == (409, {"state": "declined"})
    expired = scene(engine, asked_at=datetime.now(timezone.utc) - timedelta(days=8))
    act(api_client, expired.candidate.id)
    refused = send(api_client, expired.ask.id)
    assert (refused.status_code, refused.json()["error"]["details"]) == (409, {"state": "expired"})
    assert rows(engine, "SELECT count(*) FROM interest_signals") == [(0,)]


def test_another_persons_introduction_is_404(engine, api_client):
    s = scene(engine)
    act(api_client, make_stranger(engine))
    assert send(api_client, s.ask.id).status_code == 404


def test_the_candidate_lists_what_they_sent_and_received_with_the_quota(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)
    send(api_client, s.ask.id)
    act(api_client, s.member)
    send(api_client, s.ask.id)
    act(api_client, s.candidate.id)

    data = api_client.get("/v1/signals").json()["data"]

    assert [x["sender_side"] for x in data["sent"]] == ["candidate"]
    assert [x["sender_side"] for x in data["received"]] == ["organization"]
    assert data["received"][0]["introduction_id"] == str(s.ask.id) and data["quota"]["used"] == 1


def test_the_organization_lists_its_signals_and_the_candidate_signals_it_may_see(engine, api_client):
    s = scene(engine, accepted=True)
    asked, ask = another_ask(engine, s)
    act(api_client, asked.id)
    send(api_client, ask.id)  # a candidate signal on a still-asked introduction
    act(api_client, s.candidate.id)
    send(api_client, s.ask.id)
    act(api_client, s.member)
    send(api_client, s.ask.id)

    data = api_client.get("/v1/signals", params={"org_id": str(s.org.id)}).json()["data"]

    assert [x["sender_side"] for x in data["sent"]] == ["organization"]
    assert [x["introduction_id"] for x in data["received"]] == [str(s.ask.id)]  # not the asked candidate's
    assert data["quota"]["used"] == 1 and data["quota"]["limit"] == 3
    act(api_client, make_stranger(engine))
    assert api_client.get("/v1/signals", params={"org_id": str(s.org.id)}).status_code == 404
