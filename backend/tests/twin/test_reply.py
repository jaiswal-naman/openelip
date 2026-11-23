"""TWN-02 end to end over HTTP (plan 09-03): the employer asks, the record does not answer, the candidate approves the
relay approval with their own reply, the worker releases it and the Twin resumes. The employer reads `relayed` only
after the release, with the reply as written and `in_reply_to` the question; a reply with contact details is refused
before it is stored; rejection and expiry end in a decline. Everything is invented."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.guardian import (
    approvals,
    jobs,  # noqa: F401  (registers guardian.release)
)
from app.platform import db
from app.platform.sessions import Principal, current_user
from app.twin import logic
from tests.twin.fixtures.graph import world as w

REPLY = "I can start in 30 days, and I would like to talk about the team first."


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


def sign_in(client, user) -> None:
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


@pytest.fixture
def asked(api_client, engine, monkeypatch):
    """A question the record does not answer, held for the candidate: the world, the question and the approval."""
    w.use_fakes(monkeypatch, "unknown")
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    question = w.ask(world, "When could you start?")
    w.run_jobs()
    waiting = w.pending(engine, question)
    return type("A", (), {"world": world, "question": question, "waiting": waiting, "client": api_client})()


def employer_view(a, engine):
    sign_in(a.client, a.world.member)
    data = a.client.get("/v1/twin/questions", params={"introduction_id": str(a.world.world.introduction)}).json()[
        "data"
    ]
    (view,) = data["sessions"][0]["questions"]
    return view


def approve(a, body):
    sign_in(a.client, a.world.candidate)
    return a.client.post(f"/v1/approvals/{a.waiting.approval_id}/approve", json=body, headers=key())


def test_the_candidate_replies_and_the_employer_reads_it_only_after_the_release(api_client, engine, asked):
    sign_in(api_client, asked.world.candidate)
    listed = api_client.get("/v1/approvals", params={"kind": "twin_answer"}).json()["data"]
    (item,) = listed
    assert item["needs_reply"] is True and item["editable_fields"] == ["body"] and item["payload"]["body"] is None
    response = approve(asked, {"payload_hash": item["payload_hash"], "reply": REPLY, "save_to_record": True})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["release"] == "pending" and data["approval"]["status"] == "rejected"  # the reply is an edit
    assert data["question"] == {"question_id": str(asked.question), "state": "waiting_for_candidate"}
    assert data["relayed_to"] == "Halden Demo"
    assert data["record_proposal"]["status"] == "proposed" and data["record_proposal"]["event_id"]
    # the proposal waits for the candidate: nothing in the record changed
    assert w.one(engine, "SELECT count(*) FROM profile_facts WHERE statement = :s", s=REPLY) == 0
    event = w.rows(
        engine,
        """SELECT event_type::text AS kind, origin::text AS origin, origin_ref, after
             FROM profile_events WHERE origin = 'agent'""",
    )
    assert [(e.kind, e.origin, e.origin_ref, e.after["statement"]) for e in event] == [
        ("proposed", "agent", str(asked.question), REPLY)
    ]
    # the employer has the holding message and no reply yet
    view = employer_view(asked, engine)
    assert view["state"] == "waiting_for_candidate" and REPLY not in str(view)
    assert (
        w.one(engine, "SELECT count(*) FROM twin_messages WHERE author = 'candidate'") == 1
    )  # stored, employer-hidden
    # the worker releases the replacement and the Twin resumes
    w.settled(engine, asked.question)
    view = employer_view(asked, engine)
    assert view["state"] == "relayed" and view["answer"]["body"] == REPLY and view["answer"]["author"] == "twin"
    (final,) = w.finals(engine, asked.question)
    assert final.kind == "relayed" and final.agent_action_id is not None
    assert (
        w.one(
            engine,
            "SELECT in_reply_to FROM twin_messages WHERE author = 'twin' AND answer_kind = 'relayed'",
        )
        == asked.question
    )
    # the candidate's own reading has their reply too
    sign_in(asked.client, asked.world.candidate)
    (session,) = asked.client.get("/v1/twin/questions").json()["data"]["sessions"]
    assert session["questions"][0]["reply"]["body"] == REPLY


def test_a_reply_without_save_to_record_proposes_nothing(api_client, engine, asked):
    response = approve(asked, {"payload_hash": _hash(asked), "reply": REPLY})
    assert response.status_code == 200 and response.json()["data"]["record_proposal"] is None
    assert w.one(engine, "SELECT count(*) FROM profile_events WHERE origin = 'agent'") == 0


def _hash(a) -> str:
    from app.guardian import api as guardian

    return guardian.payload_hash(a.waiting.payload)


@pytest.mark.parametrize(
    "reply", ["Write to me at ada@example.test.", "Call +91 98765 43210.", "My salary is flexible."]
)
def test_a_reply_with_contact_details_or_pay_is_refused_and_nothing_is_stored(api_client, engine, asked, reply):
    response = approve(asked, {"payload_hash": _hash(asked), "reply": reply})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_failed" and error["details"]["reason"] == "restricted_data"
    assert w.one(engine, "SELECT count(*) FROM twin_messages WHERE author = 'candidate'") == 0
    assert w.one(engine, "SELECT status::text FROM approvals") == "pending"
    assert w.one(engine, "SELECT count(*) FROM profile_events WHERE origin = 'agent'") == 0


@pytest.mark.parametrize("body", [{}, {"reply": ""}, {"reply": "x" * 4001}])
def test_a_relay_needs_a_reply_of_one_to_four_thousand_characters(api_client, engine, asked, body):
    response = approve(asked, {"payload_hash": _hash(asked), **body})
    assert response.status_code == 422, response.text
    assert w.one(engine, "SELECT status::text FROM approvals") == "pending"


def test_a_stale_hash_rolls_the_reply_back_with_it(api_client, engine, asked):
    response = approve(asked, {"payload_hash": "0" * 64, "reply": REPLY})
    assert response.status_code == 409 and response.json()["error"]["code"] == "preview_changed"
    assert w.one(engine, "SELECT count(*) FROM twin_messages WHERE author = 'candidate'") == 0


def test_a_drafted_answer_takes_no_reply(api_client, engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    question = w.ask(world)
    w.run_jobs()
    waiting = w.pending(engine, question)
    a = type("A", (), {"world": world, "question": question, "waiting": waiting, "client": api_client})()
    response = approve(a, {"payload_hash": _hash(a), "reply": "Yes."})
    assert response.status_code == 422 and response.json()["error"]["details"]["fields"][0]["field"] == "reply"
    response = approve(a, {"payload_hash": _hash(a), "edits": {"body": "Changed."}})
    assert response.status_code == 422  # a drafted answer is approved or rejected, never edited
    response = approve(a, {"payload_hash": _hash(a)})
    assert response.status_code == 200 and "relayed_to" not in response.json()["data"]
    w.settled(engine, question)
    assert w.finals(engine, question)[0].kind == "answered"


def test_rejecting_the_relay_ends_in_a_decline(api_client, engine, asked):
    sign_in(api_client, asked.world.candidate)
    rejected = api_client.post(f"/v1/approvals/{asked.waiting.approval_id}/reject", json={}, headers=key())
    assert rejected.status_code == 200, rejected.text
    w.settled(engine, asked.question)
    view = employer_view(asked, engine)
    assert view["state"] == "declined" and view["answer"]["body"] == logic.DECLINED_TEXT
    assert w.one(engine, "SELECT count(*) FROM twin_messages WHERE answer_kind = 'relayed'") == 0


def test_an_unanswered_relay_expires_into_a_decline(api_client, engine, asked):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE status = 'pending'"))
    with db.transaction(role="worker") as conn:
        assert approvals.expire_due(conn) == 1
    w.settled(engine, asked.question)
    assert employer_view(asked, engine)["state"] == "declined"
