"""BRF-01 and BRF-02: the in-app brief inbox and one brief (GET /v1/briefs and GET /v1/briefs/{id}).

Briefs are composed on a committed connection (the superuser's), then read through the API as the real `app_api` role,
so row-level security and the grants apply. People and jobs are invented (tests/match/factories.py).
"""

import base64
import json
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief.api import compose
from app.profile import api as profile
from tests.brief.conftest import new_person
from tests.brief.world import world
from tests.match.factories import LOCAL_DATE

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)
SHAPE = {
    "id",
    "local_date",
    "status",
    "delivery",
    "decisions",
    "done_since_last_brief",
    "open_record_proposals",
    "undoable",
}


def morning_of(engine, email="ada@mail.test", *, earlier_days=(), prefix=""):
    """A person with today's brief (three matches, an email queued) and, for each number in `earlier_days`, a quiet
    brief that many days back. Returns the person, today's decision ids, every in-app brief id (newest first) and
    today's ComposeResult."""
    with engine.begin() as c:
        user, decisions = world(c, email=email, prefix=prefix)
        today = compose(c, user, LOCAL_DATE, now=MORNING)
        older = [compose(c, user, LOCAL_DATE - timedelta(days=n), now=MORNING) for n in earlier_days]
    return user, decisions, [today.in_app_brief_id, *(brief.in_app_brief_id for brief in older)], today


# ---- the inbox -------------------------------------------------------------------------------------------------


def test_the_inbox_lists_the_persons_own_briefs_newest_first_with_counts_and_delivery(
    engine, api_client, act_as, apply_links
):
    ada, _, ids, _ = morning_of(engine, earlier_days=(1, 2))
    bob_briefs = morning_of(engine, "bob@mail.test", prefix="bob-")[2]  # another person: never in ada's list
    act_as(api_client, ada)

    response = api_client.get("/v1/briefs")

    assert response.status_code == 200
    body = response.json()
    assert [b["id"] for b in body["data"]] == [str(i) for i in ids]
    assert [b["local_date"] for b in body["data"]] == ["2026-11-23", "2026-11-22", "2026-11-21"]
    assert [(b["status"], b["item_count"]) for b in body["data"]] == [
        ("composed", 3),
        ("suppressed", 0),
        ("suppressed", 0),
    ]
    assert body["data"][0]["delivery"] == {"in_app": "composed", "email": "queued", "email_sent_at": None}
    assert body["data"][1]["delivery"] == {"in_app": "suppressed", "email": None, "email_sent_at": None}
    assert body["meta"]["next_cursor"] is None and body["meta"]["limit"] == 20
    assert str(bob_briefs[0]) not in response.text


def test_delivery_is_read_from_the_outbox_row_of_the_email(engine, api_client, act_as, apply_links):
    ada, _, _, today = morning_of(engine)
    act_as(api_client, ada)
    expected = {
        "pending": "queued",
        "sending": "queued",
        "sent": "sent",
        "failed": "failed",
        "cancelled": "failed",
        "unknown": "failed",
    }

    for status, shown in expected.items():
        with engine.begin() as c:
            c.execute(
                text(
                    """UPDATE outbox SET status = CAST(:s AS outbox_status),
                              sent_at = CASE WHEN :is_sent THEN now() END WHERE id = :id"""
                ),
                {"s": status, "is_sent": status == "sent", "id": today.outbox_id},
            )
        delivery = api_client.get("/v1/briefs").json()["data"][0]["delivery"]
        assert delivery["email"] == shown, status
        assert (delivery["email_sent_at"] is not None) == (status == "sent")


def test_the_inbox_pages_with_a_cursor_and_refuses_one_it_did_not_issue(engine, api_client, act_as, apply_links):
    ada, _, ids, _ = morning_of(engine, earlier_days=(1, 2, 3, 4))
    act_as(api_client, ada)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = api_client.get("/v1/briefs", params=params).json()
        seen += [b["id"] for b in body["data"]]
        pages += 1
        cursor = body["meta"]["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(i) for i in ids] and pages == 3
    for bad in ("garbage", base64.urlsafe_b64encode(json.dumps(["not-a-date", "x"]).encode()).decode(), "e30"):
        refused = api_client.get("/v1/briefs", params={"cursor": bad})
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_request", bad
    assert api_client.get("/v1/briefs", params={"limit": 0}).status_code == 422
    assert api_client.get("/v1/briefs", params={"limit": 101}).status_code == 422


def test_an_empty_inbox_is_an_empty_list(engine, api_client, act_as):
    act_as(api_client, new_person(engine))

    response = api_client.get("/v1/briefs")

    assert response.status_code == 200 and response.json()["data"] == []


def test_the_inbox_needs_a_session(api_client):
    assert api_client.get("/v1/briefs").status_code == 401
    assert api_client.get(f"/v1/briefs/{uuid4()}").status_code == 401


# ---- one brief -------------------------------------------------------------------------------------------------


def test_one_brief_has_its_decisions_with_their_actions_and_what_was_done(engine, api_client, act_as, apply_links):
    ada, decisions, ids, _ = morning_of(engine)
    act_as(api_client, ada)

    response = api_client.get(f"/v1/briefs/{ids[0]}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == SHAPE and data["id"] == str(ids[0]) and data["local_date"] == "2026-11-23"
    assert data["delivery"] == {"in_app": "composed", "email": "queued", "email_sent_at": None}
    assert [d["ref"] for d in data["decisions"]] == [{"decision_id": str(d)} for d in decisions]
    first = data["decisions"][0]
    assert first["kind"] == "match" and first["title"] == "Role 1 at Initech Demo" and first["why"] == "Summary 1."
    assert [a["action"] for a in first["actions"]] == ["open", "save", "dismiss"]
    assert first["actions"][1] == {
        "action": "save",
        "method": "POST",
        "path": f"/v1/matches/{decisions[0]}/feedback",
        "body": {"kind": "save"},
    }
    assert data["open_record_proposals"] == 0 and data["done_since_last_brief"] == []


def test_a_proposal_that_arrives_after_the_brief_is_still_waiting_when_it_is_read(
    engine, api_client, act_as, apply_links
):
    ada, _, ids, _ = morning_of(engine)
    with engine.begin() as c:
        profile.propose_change(
            c, ada, origin="chat", entity="fact", operation="create", after={"kind": "skill", "statement": "Go"}
        )
    act_as(api_client, ada)

    data = api_client.get(f"/v1/briefs/{ids[0]}").json()["data"]

    assert data["open_record_proposals"] == 1
    assert data["decisions"][-1]["kind"] == "record_proposals"
    assert data["decisions"][-1]["actions"] == [
        {"action": "review", "method": "GET", "path": "/v1/me/events?status=proposed", "body": None}
    ]


def test_another_persons_brief_the_email_brief_and_an_unknown_id_are_all_a_404(engine, api_client, act_as, apply_links):
    ada, _, ids, _ = morning_of(engine)
    bob = morning_of(engine, "bob@mail.test", prefix="bob-")[0]
    with engine.connect() as c:
        email_brief = c.execute(
            text("SELECT id FROM briefs WHERE channel = 'email' AND user_id = :u"), {"u": ada}
        ).scalar_one()
    act_as(api_client, bob)

    for brief_id in (ids[0], email_brief, uuid4()):
        refused = api_client.get(f"/v1/briefs/{brief_id}")
        assert refused.status_code == 404 and refused.json()["error"]["code"] == "not_found", brief_id
    assert "Role 1" not in api_client.get(f"/v1/briefs/{ids[0]}").text
    assert api_client.get("/v1/briefs/not-an-id").status_code == 422


def test_a_quiet_days_brief_is_empty_and_says_so(engine, api_client, act_as, apply_links):
    ada, _, ids, _ = morning_of(engine, earlier_days=(1,))
    act_as(api_client, ada)

    data = api_client.get(f"/v1/briefs/{ids[1]}").json()["data"]

    assert data["status"] == "suppressed" and data["decisions"] == [] and data["delivery"]["email"] is None


def test_the_openapi_document_marks_the_third_party_text_and_lists_the_routes(api_client):
    spec = api_client.get("/openapi.json").json()

    assert {"get"} <= set(spec["paths"]["/v1/briefs"]) and "get" in spec["paths"]["/v1/briefs/{brief_id}"]
    assert set(spec["paths"]["/v1/briefs/unsubscribe"]) == {"post"}
    schemas = spec["components"]["schemas"]
    assert schemas["BriefDecision"]["properties"]["title"]["x-untrusted-text"] is True
    assert set(schemas["BriefDetail"]["properties"]) == SHAPE
