"""HIR-04: GET /v1/introductions and GET /v1/introductions/{id}, and the decline route (plan 06-04).

Rows are the invented ones of tests/hiring/factories.py, committed, then read through the API as the real `app_api`
role, so row-level security and the grants apply. The helpers at the top are shared by the other test files of this plan.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.hiring import share
from app.platform.sessions import Principal, current_user
from tests.hiring import world
from tests.hiring.factories import (
    Ask,
    Candidate,
    Org,
    Role,
    make_accepted,
    make_ask,
    make_candidate,
    make_member,
    make_open_role,
    make_org,
)

VERSION = "introduction-2026-11"


@dataclass(frozen=True)
class Scene:
    org: Org
    member: UUID  # a second active member of the organization
    role: Role
    candidate: Candidate
    ask: Ask


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


def act(client, user: UUID) -> None:
    """The API client acts as this person from now on."""
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


def scene(engine, *, timezone_name: str = "UTC", accepted: bool = False, asked_at: datetime | None = None) -> Scene:
    """An organization with two members and an open role, and one candidate who was asked (or has accepted)."""
    with engine.begin() as c:
        org = make_org(c, timezone=timezone_name)
        member = make_member(c, org).user
        role = make_open_role(c, org)
        candidate = make_candidate(c)
        ask = make_ask(c, role, candidate, asked_at=asked_at)
        if accepted:
            make_accepted(c, role, candidate, ask=ask)
    return Scene(org, member, role, candidate, ask)


def another_ask(
    engine, s: Scene, *, accepted: bool = False, name: str = "Demo Second Candidate"
) -> tuple[Candidate, Ask]:
    """A second candidate asked about the same role."""
    with engine.begin() as c:
        candidate = make_candidate(c, name=name)
        ask = make_ask(c, s.role, candidate)
        if accepted:
            make_accepted(c, s.role, candidate, ask=ask)
    return candidate, ask


def answer_as_employer(engine, s: Scene, intro: UUID, verdict: str = "no", reason: str = "Skills gap") -> None:
    """The employer's answer, in plain SQL under the employer_answer policy (the route is tested elsewhere)."""
    with engine.begin() as c:
        world.act_as(c, "app_api", s.member)
        c.execute(
            text(
                """UPDATE introductions SET employer_answer = CAST(:a AS employer_answer), employer_answer_reason = :r,
                       employer_answered_by = :me WHERE id = :i"""
            ),
            {"a": verdict, "r": reason, "me": s.member, "i": intro},
        )


def accept_body(ask: Ask) -> dict[str, str]:
    return {"share_preview_hash": share.preview_hash(ask.preview), "consent_text_version": VERSION}


def rows(engine, sql: str, **params) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), params).all())


# ---- the candidate's list and detail ---------------------------------------------------------------------------


def test_a_candidate_lists_only_their_own_introductions_with_the_organization_and_role(engine, api_client):
    s = scene(engine)
    other, _ = another_ask(engine, s)  # someone else's ask: never in this list
    act(api_client, s.candidate.id)

    body = api_client.get("/v1/introductions").json()

    assert [item["id"] for item in body["data"]] == [str(s.ask.id)]
    item = body["data"][0]
    assert (item["status"], item["organization_name"], item["role_title"]) == ("asked", s.org.name, s.role.title)
    assert item["responded_at"] is None and item["employer_answer"] is None and item["employer_answered_at"] is None
    assert item["expires_at"] is not None
    assert body["meta"]["next_cursor"] is None and body["meta"]["limit"] == 20


def test_the_list_pages_newest_first_with_a_cursor(engine, api_client):
    s = scene(engine)
    with engine.begin() as c:
        second = make_ask(
            c, make_open_role(c, s.org, title="Second role"), s.candidate, asked_at=datetime.now(timezone.utc)
        )
    act(api_client, s.candidate.id)

    first = api_client.get("/v1/introductions", params={"limit": 1}).json()
    assert [i["id"] for i in first["data"]] == [str(second.id)]
    rest = api_client.get("/v1/introductions", params={"limit": 1, "cursor": first["meta"]["next_cursor"]}).json()
    assert [i["id"] for i in rest["data"]] == [str(s.ask.id)]
    assert rest["meta"]["next_cursor"] is None


def test_the_ask_shows_the_exact_preview_its_hash_the_wording_the_organization_and_the_pay(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)

    response = api_client.get(f"/v1/introductions/{s.ask.id}")

    data = response.json()["data"]
    assert response.status_code == 200 and "view" not in data
    assert data["share_preview"] == s.ask.preview
    assert data["share_preview_hash"] == share.preview_hash(s.ask.preview)
    assert data["consent_text_version"] == VERSION and data["status"] == "asked"
    assert data["organization"] == {"id": str(s.org.id), "name": s.org.name, "is_synthetic": True}
    assert data["role"]["id"] == str(s.role.id) and data["role"]["title"] == s.role.title
    assert data["role"]["pay_range"] == {"min": 80_000, "max": 120_000, "currency": "USD"}
    assert data["has_employer_signal"] is False
    assert s.org.name in data["terms"] and "cannot be undone" in data["terms"] and "5 business days" in data["terms"]
    assert "Twin" not in data["terms"]  # that sentence waits for Phase 9
    assert data["employer_answer"] is None and data["employer_answer_reason"] is None
    assert data["employer_answered_at"] is None and data["responded_at"] is None


def test_the_candidate_sees_the_employers_answer_after_it_is_given_and_null_before(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)
    before = api_client.get(f"/v1/introductions/{s.ask.id}").json()["data"]
    assert before["status"] == "accepted" and before["responded_at"] is not None
    assert (before["employer_answer"], before["employer_answer_reason"], before["employer_answered_at"]) == (
        None,
        None,
        None,
    )

    answer_as_employer(engine, s, s.ask.id, "yes", "Strong fit")

    after = api_client.get(f"/v1/introductions/{s.ask.id}").json()["data"]
    assert (after["employer_answer"], after["employer_answer_reason"]) == ("yes", "Strong fit")
    assert after["employer_answered_at"] is not None
    listed = api_client.get("/v1/introductions").json()["data"][0]
    assert (listed["employer_answer"], listed["employer_answered_at"]) == ("yes", after["employer_answered_at"])


def test_a_signal_from_the_employer_shows_on_the_ask(engine, api_client):
    s = scene(engine, accepted=True)
    with engine.begin() as c:
        world.signal(c, "organization", s.candidate.id, s.org.id, s.role.id, intro=s.ask.id, sent_by=s.member)
    act(api_client, s.candidate.id)
    assert api_client.get(f"/v1/introductions/{s.ask.id}").json()["data"]["has_employer_signal"] is True


def test_another_persons_introduction_is_404_on_every_route(engine, api_client):
    s = scene(engine)
    stranger = make_stranger(engine)
    act(api_client, stranger)
    path = f"/v1/introductions/{s.ask.id}"
    assert api_client.get(path).status_code == 404
    assert api_client.post(f"{path}/accept", json=accept_body(s.ask), headers=key()).status_code == 404
    assert api_client.post(f"{path}/decline", headers=key()).status_code == 404
    assert api_client.get("/v1/introductions").json()["data"] == []
    assert rows(engine, "SELECT status::text FROM introductions") == [("asked",)]


def make_stranger(engine) -> UUID:
    with engine.begin() as c:
        return make_candidate(c, name="Demo Stranger").id


def test_an_employer_member_reads_an_accepted_introduction_with_what_was_shared(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)

    data = api_client.get(f"/v1/introductions/{s.ask.id}").json()["data"]

    assert data["shared"] == s.ask.preview["on_accept"] and data["status"] == "accepted"
    assert data["answer_due_at"] is not None and data["is_overdue"] is False and data["email"] is None
    assert "share_preview" not in data and "on_employer_yes" not in str(data)
    listed = api_client.get("/v1/introductions", params={"role_id": str(s.role.id)}).json()["data"]
    assert [i["id"] for i in listed] == [str(s.ask.id)]


# ---- decline ---------------------------------------------------------------------------------------------------


def test_declining_is_private_and_recorded(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)

    response = api_client.post(f"/v1/introductions/{s.ask.id}/decline", headers=key())

    assert response.status_code == 200
    intro = response.json()["data"]["introduction"]
    assert intro["status"] == "declined" and intro["responded_at"] is not None
    assert rows(engine, "SELECT status::text, responded_at IS NOT NULL FROM introductions") == [("declined", True)]
    audit = rows(
        engine,
        "SELECT event_type, actor_type::text, subject_user_id, organization_id, details FROM audit_log "
        "WHERE event_type = 'hiring.introduction_declined'",
    )
    assert len(audit) == 1 and audit[0].organization_id == s.org.id and audit[0].subject_user_id == s.candidate.id
    assert set(audit[0].details) == {"introduction_id", "role_id"}
    assert rows(engine, "SELECT count(*) FROM shortlist_entries") == [(0,)]
    assert rows(engine, "SELECT count(*) FROM outbox WHERE template = 'introduction_accepted_v1'") == [(0,)]
    # the employer learns nothing: not in the list, not by id
    act(api_client, s.member)
    assert api_client.get("/v1/introductions", params={"org_id": str(s.org.id)}).json()["data"] == []
    assert api_client.get(f"/v1/introductions/{s.ask.id}").status_code == 404


def test_declining_after_accepting_is_refused_and_changes_nothing(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)

    refused = api_client.post(f"/v1/introductions/{s.ask.id}/decline", headers=key())

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "already_resolved"
    assert refused.json()["error"]["details"] == {"resolution": "accepted"}
    assert rows(engine, "SELECT status::text FROM introductions") == [("accepted",)]


def test_an_expired_ask_cannot_be_declined_or_accepted(engine, api_client):
    s = scene(engine, asked_at=datetime.now(timezone.utc) - timedelta(days=8))
    act(api_client, s.candidate.id)
    for path, body in (("decline", None), ("accept", accept_body(s.ask))):
        refused = api_client.post(f"/v1/introductions/{s.ask.id}/{path}", json=body, headers=key())
        assert refused.status_code == 409, path
        assert refused.json()["error"]["code"] == "conflict"
        assert refused.json()["error"]["details"] == {"state": "expired"}
    assert rows(engine, "SELECT status::text FROM introductions") == [("asked",)]


def test_a_withdrawn_ask_is_conflict_closed(engine, api_client):
    s = scene(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE introductions SET status = 'withdrawn'"))
    act(api_client, s.candidate.id)
    refused = api_client.post(f"/v1/introductions/{s.ask.id}/decline", headers=key())
    assert (refused.status_code, refused.json()["error"]["details"]) == (409, {"state": "closed"})


def test_a_write_without_a_key_is_refused(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    assert api_client.post(f"/v1/introductions/{s.ask.id}/decline").status_code == 400
