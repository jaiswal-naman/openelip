"""HIR-02: /v1/roles over HTTP (plan 06-03): create with extraction, list, get, patch, confirm, publish, close.

The API opens its own connections as the real app_api role (and the worker role for publish and close), so row-level
security and the grants are in force and what a test commits stays: these tests use api_client, build rows with the
06-02 factories on a committing connection and sign a person in by overriding the session dependency. The model is the
fake provider answering from the invented recording of 06-02. Everything here is invented.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform import config
from app.platform.llm import fake
from app.platform.sessions import Principal, current_user
from tests.hiring import world
from tests.hiring.factories import make_accepted, make_candidate, make_open_role, make_org, make_role

RECORDING = Path(__file__).parent / "fixtures" / "intake" / "invented_answer_three.json"
DESCRIPTION = (
    "Café Halden Demo ☕ is hiring a data engineer 🚀.\n"
    "What you'll need\n"
    "3+ years of Python.\n"
    "Strong SQL skills.\n"
    "Experience with Airflow is a plus.\n"
    "We offer free lunch."
)
PAY = {"min": 80_000, "max": 120_000, "currency": "USD"}


@pytest.fixture
def provider(monkeypatch):
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    settings = config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake"})
    monkeypatch.setattr(config, "get", lambda: settings)
    fake.replay_from(RECORDING)
    yield
    fake.replay_from(None)


@pytest.fixture
def desk(engine, api_client):
    """An organization with its owner, another organization with its owner, and a client that signs people in."""
    d = SimpleNamespace(client=api_client)
    with engine.begin() as conn:
        d.org, d.other = make_org(conn), make_org(conn, "Brightwell Demo")
    d.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    d.sign_in(d.org.owner)
    return d


def key():
    return {"Idempotency-Key": str(uuid4())}


def ok(response, status=200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def post_role(desk, **over):
    body = {"org_id": str(desk.org.id), "title": "Data engineer", "description": DESCRIPTION, **over}
    return desk.client.post("/v1/roles", json=body, headers=key())


def confirm_all(desk, role):
    items = [
        {"kind": r["kind"], "text": r["text"], "importance": r["importance"], "span": r["span"], "is_confirmed": True}
        for r in role["requirements"]
    ]
    return desk.client.put(f"/v1/roles/{role['id']}/requirements", json={"requirements": items}, headers=key())


# ---- create --------------------------------------------------------------------------------------------------------


def test_creating_a_role_returns_a_draft_with_extracted_requirements_blockers_and_extraction(desk, provider):
    role = ok(post_role(desk, city="Pune", country="IN", work_arrangements=["hybrid"]), 201)

    assert (role["status"], role["pay_range"], role["can_publish"]) == ("draft", None, False)
    assert role["blockers"] == ["pay_range_required", "requirements_unconfirmed"]
    assert role["extraction"] == {"state": "extracted", "dropped": 0}
    assert (role["country"], role["city"], role["work_arrangements"]) == ("IN", "Pune", ["hybrid"])
    assert [r["text"] for r in role["requirements"]] == [
        "3+ years of Python.",
        "Strong SQL skills",
        "Airflow is a plus",
    ]
    first = role["requirements"][0]
    assert set(first) == {"id", "kind", "text", "importance", "span", "quote", "is_confirmed"}
    assert DESCRIPTION[first["span"]["start"] : first["span"]["end"]] == first["quote"] == first["text"]
    assert first["is_confirmed"] is False


def test_repeating_a_create_with_the_same_key_replays_it_and_makes_one_role(desk, engine, provider):
    headers = key()
    body = {"org_id": str(desk.org.id), "title": "Data engineer", "description": DESCRIPTION}

    first = desk.client.post("/v1/roles", json=body, headers=headers)
    again = desk.client.post("/v1/roles", json=body, headers=headers)

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json()["data"] == first.json()["data"] and again.json()["meta"]["idempotent_replay"] is True
    with engine.connect() as conn:
        assert world.count(conn, "roles") == 1


def test_with_no_model_a_role_is_created_with_no_requirements_and_says_ai_disabled(desk, monkeypatch):
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": "", "LLM_PROVIDER": ""}
    monkeypatch.setattr(config, "get", lambda: config.load({**os.environ, **blank, "APP_ENV": "test"}))

    role = ok(post_role(desk), 201)

    assert role["extraction"] == {"state": "ai_disabled", "dropped": 0} and role["requirements"] == []


@pytest.mark.parametrize(
    "over",
    [
        {"title": " "},
        {"description": ""},
        {"pay_range": {"min": 5, "max": 4, "currency": "USD"}},
        {"pay_range": {"min": 1, "max": 2, "currency": "usd"}},
        {"country": "india"},
        {"work_arrangements": ["underwater"]},
        {"seniority": "wizard"},
        {"version": 3},
    ],
)
def test_bad_fields_are_validation_failed(desk, over):
    refused(post_role(desk, **over), 422, "validation_failed")


def test_a_write_needs_a_key_and_a_session(desk):
    body = {"org_id": str(desk.org.id), "title": "T", "description": "D"}
    refused(desk.client.post("/v1/roles", json=body), 400, "idempotency_key_missing")
    desk.client.app.dependency_overrides.clear()
    refused(desk.client.post("/v1/roles", json=body, headers=key()), 401, "unauthenticated")
    refused(desk.client.get(f"/v1/roles?org_id={desk.org.id}"), 401, "unauthenticated")


# ---- the whole path ------------------------------------------------------------------------------------------------


def test_describe_confirm_set_pay_publish_and_close(desk, provider, engine):
    role = ok(post_role(desk), 201)
    rid = role["id"]

    # blocked: no pay range, nothing confirmed
    error = refused(desk.client.post(f"/v1/roles/{rid}/publish", headers=key()), 422, "pay_range_required")
    assert error["details"] == {"blockers": ["pay_range_required", "requirements_unconfirmed"]}

    patched = ok(desk.client.patch(f"/v1/roles/{rid}", json={"pay_range": PAY, "seniority": "senior"}, headers=key()))
    assert patched["pay_range"] == PAY and patched["seniority"] == "senior" and patched["extraction"] is None
    assert patched["blockers"] == ["requirements_unconfirmed"]

    confirmed = ok(confirm_all(desk, role))
    assert confirmed["can_publish"] is True and confirmed["blockers"] == []
    assert ok(desk.client.get(f"/v1/roles/{rid}"))["can_publish"] is True

    opened = ok(desk.client.post(f"/v1/roles/{rid}/publish", headers=key()))
    assert opened["status"] == "open" and isinstance(opened["job_id"], int) and opened["opened_at"]
    assert opened["can_publish"] is False
    with engine.connect() as conn:
        assert world.count(conn, "queue_jobs WHERE kind = 'hiring.ask_candidates'") == 1

    twice = refused(desk.client.post(f"/v1/roles/{rid}/publish", headers=key()), 409, "conflict")
    assert twice["details"] == {"state": "open"}
    changed = refused(desk.client.patch(f"/v1/roles/{rid}", json={"title": "New"}, headers=key()), 409, "conflict")
    assert changed["details"] == {"state": "open"}

    closed = ok(desk.client.post(f"/v1/roles/{rid}/close", headers=key()))
    assert closed["status"] == "closed" and closed["closed_at"]
    again = refused(desk.client.post(f"/v1/roles/{rid}/close", headers=key()), 409, "conflict")
    assert again["details"] == {"state": "closed"}


def test_publish_with_the_same_key_replays_and_opens_one_job(desk, engine):
    with engine.begin() as conn:
        draft = make_role(conn, desk.org, confirmed=True, pay=(80_000, 120_000, "USD"))
    headers = key()

    first = desk.client.post(f"/v1/roles/{draft.id}/publish", headers=headers)
    again = desk.client.post(f"/v1/roles/{draft.id}/publish", headers=headers)

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["meta"]["idempotent_replay"] is True
    with engine.connect() as conn:
        assert (
            world.count(conn, "jobs") == 1 and world.count(conn, "queue_jobs WHERE kind = 'hiring.ask_candidates'") == 1
        )


def test_publish_is_a_conflict_while_an_answer_is_overdue(desk, engine):
    with engine.begin() as conn:
        owed = make_accepted(conn, make_open_role(conn, desk.org), make_candidate(conn))
        world.overdue(conn, owed.id)
        draft = make_role(conn, desk.org, confirmed=True, pay=(80_000, 120_000, "USD"))

    error = refused(desk.client.post(f"/v1/roles/{draft.id}/publish", headers=key()), 409, "conflict")

    assert error["details"] == {"state": "answers_overdue"}


def test_the_list_shows_status_and_what_is_owed_and_get_shows_one_role(desk, engine):
    with engine.begin() as conn:
        live = make_open_role(conn, desk.org, title="Platform engineer")
        owed = make_accepted(conn, live, make_candidate(conn))
        world.overdue(conn, owed.id)
        make_role(conn, desk.org, title="Draft role")

    listed = ok(desk.client.get(f"/v1/roles?org_id={desk.org.id}"))

    by_title = {r["title"]: r for r in listed}
    assert set(by_title) == {"Platform engineer", "Draft role"}
    platform = by_title["Platform engineer"]
    assert (platform["status"], platform["said_yes"], platform["answers_owed"], platform["answers_overdue"]) == (
        "open",
        1,
        1,
        1,
    )
    assert platform["updated_at"] and by_title["Draft role"]["status"] == "draft"
    assert ok(desk.client.get(f"/v1/roles/{live.id}"))["title"] == "Platform engineer"


def test_a_pay_range_with_min_above_max_is_refused_on_patch_too(desk, engine):
    with engine.begin() as conn:
        draft = make_role(conn, desk.org)

    bad = {"pay_range": {"min": 9, "max": 3, "currency": "EUR"}}
    refused(desk.client.patch(f"/v1/roles/{draft.id}", json=bad, headers=key()), 422, "validation_failed")
    refused(desk.client.patch(f"/v1/roles/{draft.id}", json={"title": None}, headers=key()), 422, "validation_failed")
    cleared = ok(desk.client.patch(f"/v1/roles/{draft.id}", json={"pay_range": None, "city": None}, headers=key()))
    assert cleared["pay_range"] is None and cleared["city"] is None


def test_more_than_thirty_requirements_are_validation_failed(desk, engine):
    with engine.begin() as conn:
        draft = make_role(conn, desk.org)
    items = [{"text": f"Skill {n}", "importance": "preferred", "is_confirmed": True} for n in range(31)]

    refused(
        desk.client.put(f"/v1/roles/{draft.id}/requirements", json={"requirements": items}, headers=key()),
        422,
        "validation_failed",
    )


def test_typed_items_have_no_span_and_a_stale_span_is_dropped(desk, engine):
    with engine.begin() as conn:
        draft = make_role(conn, desk.org)
    items = [
        {"text": "Right to work in India", "kind": "authorization", "importance": "required", "is_confirmed": True},
        {"text": "Not in the text", "importance": "required", "is_confirmed": True, "span": {"start": 0, "end": 5}},
    ]

    done = ok(desk.client.put(f"/v1/roles/{draft.id}/requirements", json={"requirements": items}, headers=key()))

    assert [(r["span"], r["quote"], r["kind"]) for r in done["requirements"]] == [
        (None, None, "authorization"),
        (None, None, "other"),
    ]


# ---- other organizations ---------------------------------------------------------------------------------------------


def test_every_route_is_not_found_for_a_role_of_another_organization(desk, engine):
    with engine.begin() as conn:
        role = make_role(conn, desk.org, confirmed=True, pay=(80_000, 120_000, "USD"))
    desk.sign_in(desk.other.owner)
    rid = role.id

    for response in (
        desk.client.get(f"/v1/roles/{rid}"),
        desk.client.get(f"/v1/roles/{rid}/preview"),
        desk.client.get(f"/v1/roles?org_id={desk.org.id}"),
        desk.client.patch(f"/v1/roles/{rid}", json={"title": "Mine now"}, headers=key()),
        desk.client.put(f"/v1/roles/{rid}/requirements", json={"requirements": []}, headers=key()),
        desk.client.post(f"/v1/roles/{rid}/publish", headers=key()),
        desk.client.post(f"/v1/roles/{rid}/close", headers=key()),
        post_role(desk),  # naming an organization the caller does not belong to
    ):
        refused(response, 404, "not_found")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status::text FROM roles WHERE id = :id"), {"id": rid}).scalar_one() == "draft"
