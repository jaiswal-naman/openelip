"""BRF-03: the brief schedule and the time zone on GET and PATCH /v1/me, and `capabilities` (API.md 2.3).

The API acts as a signed-in person (conftest.py's act_as); one test goes through the real emailed code and cookie.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform import auth, config, sessions
from app.platform.llm import registry
from tests.brief.conftest import new_person


def patch(client, body, key=None):
    return client.patch("/v1/me", json=body, headers={"Idempotency-Key": key or uuid4().hex})


def stored(engine, person):
    with engine.connect() as c:
        row = c.execute(
            text("SELECT timezone, brief_frequency::text AS frequency, brief_weekday FROM users WHERE id = :u"),
            {"u": person},
        ).one()
    return row.timezone, row.frequency, row.brief_weekday


@pytest.fixture
def ada(engine, api_client, act_as):
    person = new_person(engine)
    act_as(api_client, person)
    return person


def with_provider(monkeypatch, name, **keys):
    """Run the rest of the test with this model provider configured (no key unless one is given)."""
    fields = {"anthropic_api_key": None, "openai_api_key": None, "llm_base_url": None, **keys}
    settings = replace(config.get(), llm_provider=name, **fields)
    monkeypatch.setattr(config, "get", lambda: settings)
    return settings


# ---- the schedule ----------------------------------------------------------------------------------------------


def test_a_new_account_gets_a_daily_brief_and_says_so(api_client, ada):
    me = api_client.get("/v1/me").json()["data"]

    assert me["brief"] == {"frequency": "daily", "weekday": None}


def test_weekly_on_a_chosen_weekday_is_stored_and_read_back(api_client, engine, ada):
    changed = patch(api_client, {"brief": {"frequency": "weekly", "weekday": 3}})

    assert changed.status_code == 200
    assert changed.json()["data"]["brief"] == {"frequency": "weekly", "weekday": 3}
    assert stored(engine, ada) == ("UTC", "weekly", 3)
    assert api_client.get("/v1/me").json()["data"]["brief"] == {"frequency": "weekly", "weekday": 3}


def test_paused_and_daily_keep_no_weekday(api_client, engine, ada):
    patch(api_client, {"brief": {"frequency": "weekly", "weekday": 6}})

    assert patch(api_client, {"brief": {"frequency": "paused"}}).json()["data"]["brief"] == {
        "frequency": "paused",
        "weekday": None,
    }
    assert stored(engine, ada)[1:] == ("paused", None)
    assert patch(api_client, {"brief": {"frequency": "daily"}}).status_code == 200
    assert stored(engine, ada)[1:] == ("daily", None)


@pytest.mark.parametrize(
    "brief",
    [
        {"frequency": "weekly"},
        {"frequency": "weekly", "weekday": 0},
        {"frequency": "weekly", "weekday": 8},
        {"frequency": "weekly", "weekday": "monday"},
        {"frequency": "daily", "weekday": 2},
        {"frequency": "paused", "weekday": 1},
        {"frequency": "monthly"},
        {"weekday": 3},
        {},
    ],
)
def test_a_schedule_the_table_would_refuse_is_a_422_and_changes_nothing(api_client, engine, ada, brief):
    refused = patch(api_client, {"brief": brief})

    assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_failed"
    assert stored(engine, ada) == ("UTC", "daily", None)


# ---- the time zone ---------------------------------------------------------------------------------------------


def test_the_time_zone_is_stored_and_a_name_zoneinfo_cannot_load_is_a_422(api_client, engine, ada):
    assert patch(api_client, {"timezone": "Asia/Kolkata"}).json()["data"]["timezone"] == "Asia/Kolkata"
    assert stored(engine, ada) == ("Asia/Kolkata", "daily", None)
    for name in ("Mars/Olympus_Mons", "../etc/passwd", "", "UTC\n", 5):
        refused = patch(api_client, {"timezone": name})
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_failed", name
    assert stored(engine, ada)[0] == "Asia/Kolkata"


def test_the_time_zone_the_schedule_and_a_consent_change_together_or_not_at_all(api_client, engine, ada):
    whole = patch(
        api_client,
        {
            "timezone": "Europe/Berlin",
            "brief": {"frequency": "weekly", "weekday": 1},
            "consents": [{"purpose": "brief_email", "granted": True, "text_version": "v1"}],
        },
    )
    assert whole.status_code == 200
    data = whole.json()["data"]
    assert (data["timezone"], data["brief"]) == ("Europe/Berlin", {"frequency": "weekly", "weekday": 1})
    assert {c["purpose"]: c["granted"] for c in data["consents"]}["brief_email"] is True

    # a consent the server refuses (an AI wording that is not the one in force) takes the whole request with it
    refused = patch(
        api_client,
        {
            "timezone": "Asia/Tokyo",
            "brief": {"frequency": "paused"},
            "consents": [{"purpose": "ai_processing", "granted": True, "text_version": "stale-wording"}],
        },
    )
    assert refused.status_code == 409
    assert stored(engine, ada) == ("Europe/Berlin", "weekly", 1)


def test_a_consents_only_patch_still_works_and_an_empty_one_does_not(api_client, ada):
    consent = {"purpose": "brief_email", "granted": True, "text_version": "v1"}

    assert patch(api_client, {"consents": [consent]}).status_code == 200
    for empty in ({}, {"consents": []}, {"brief": None}):
        assert patch(api_client, empty).status_code == 422, empty


def test_the_idempotency_rule_of_the_existing_handler_still_applies(api_client, engine, ada):
    body = {"brief": {"frequency": "weekly", "weekday": 2}}

    missing = api_client.patch("/v1/me", json=body)
    first = patch(api_client, body, key="settings-key-0001")
    replayed = patch(api_client, body, key="settings-key-0001")
    reused = patch(api_client, {"brief": {"frequency": "paused"}}, key="settings-key-0001")

    assert missing.status_code == 400 and missing.json()["error"]["code"] == "idempotency_key_missing"
    assert first.status_code == 200 and "idempotent_replay" not in first.json()["meta"]
    assert replayed.status_code == 200 and replayed.json()["meta"]["idempotent_replay"] is True
    assert reused.status_code == 422 and reused.json()["error"]["code"] == "idempotency_key_reused"
    assert stored(engine, ada)[1:] == ("weekly", 2)


# ---- what GET /v1/me reports -----------------------------------------------------------------------------------


def test_the_brief_email_is_the_brief_email_consent(api_client, ada):
    def on():
        return {c["purpose"]: c["granted"] for c in api_client.get("/v1/me").json()["data"]["consents"]}["brief_email"]

    assert on() is False
    patch(api_client, {"consents": [{"purpose": "brief_email", "granted": True, "text_version": "v1"}]})
    assert on() is True
    patch(api_client, {"consents": [{"purpose": "brief_email", "granted": False}]})
    assert on() is False


def test_capabilities_list_the_tasks_with_a_usable_route_and_the_evidence_task_needs_one(api_client, ada, monkeypatch):
    with_provider(monkeypatch, "fake")
    with_fake = api_client.get("/v1/me").json()["data"]["capabilities"]
    with_provider(monkeypatch, "anthropic")  # no key: nothing can be called
    with_none = api_client.get("/v1/me").json()["data"]["capabilities"]
    keyed = with_provider(monkeypatch, "anthropic", anthropic_api_key="sk-invented-for-the-test")
    with_a_key = api_client.get("/v1/me").json()["data"]["capabilities"]

    assert "match.evidence.score" in with_fake
    assert with_none == []
    assert with_a_key == [route.task for route in registry.active_routes(keyed)]  # what the gateway would call


def test_signing_in_reports_the_schedule_and_the_capabilities_too(api_client, engine, monkeypatch):
    with_provider(monkeypatch, "fake")
    auth._recent.clear()
    assert api_client.post("/v1/auth/codes", json={"email": "grace@mail.test"}).status_code == 202
    with engine.connect() as c:
        sealed = c.execute(text("SELECT payload->>'code_sealed' FROM outbox")).scalar_one()
    signed_in = api_client.post(
        "/v1/auth/sessions", json={"email": "grace@mail.test", "code": sessions.open_sealed(sealed)}
    )

    assert signed_in.status_code == 201
    data = signed_in.json()["data"]
    assert data["brief"] == {"frequency": "daily", "weekday": None}
    assert "match.evidence.score" in data["capabilities"]
    # and the cookie that sign-in set is a real session for the new routes, with no dependency override
    changed = patch(api_client, {"brief": {"frequency": "weekly", "weekday": 5}})
    assert changed.status_code == 200 and changed.json()["data"]["brief"]["weekday"] == 5
    assert api_client.get("/v1/me").json()["data"]["brief"] == {"frequency": "weekly", "weekday": 5}
