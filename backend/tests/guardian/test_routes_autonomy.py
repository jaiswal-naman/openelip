"""Plan 07-05, GRD-01, GRD-04: GET and PUT /v1/autonomy over HTTP.

Follow-ups and applications are never automatic: a level the type does not allow refuses the whole body and saves
nothing. The version is a hash string sent as the ETag and checked from If-Match. Everything is invented.
"""

from dataclasses import replace

import pytest

from app.platform import config, db
from tests.guardian.test_routes_approvals import key, ok, refused, scalar, sign_in
from tests.match.factories import make_user


@pytest.fixture
def person(api_client, engine):
    with engine.begin() as conn:
        user = make_user(conn)
        other = make_user(conn)
    sign_in(api_client, user)
    return type("P", (), {"client": api_client, "engine": engine, "user": user, "other": other})()


def put(person, body, *, if_match=None, headers=None):
    sent = dict(headers or key())
    if if_match is not None:
        sent["If-Match"] = if_match
    return person.client.put("/v1/autonomy", json=body, headers=sent)


def stored(engine):
    from tests.guardian.support import rows

    return {r[0]: r[1] for r in rows(engine, "SELECT action_type::text, level::text FROM autonomy_settings")}


def test_get_returns_all_three_rows_the_window_the_default_budget_and_an_etag(person):
    response = person.client.get("/v1/autonomy")
    data = ok(response)
    assert data["actions"] == {
        "submit_application": {"level": "ask_first", "allowed_levels": ["ask_first"]},
        "send_follow_up": {"level": "ask_first", "allowed_levels": ["ask_first", "never"]},
        "twin_answer": {"level": "ask_first", "allowed_levels": ["ask_first", "tell_me_after", "never"]},
    }
    assert data["undo_window_minutes"] == 10 and data["campaign_budget_default_usd"] == "0.75"
    assert data["profile_sharing"] == "Only when you accept an introduction."
    assert isinstance(data["version"], str) and response.headers["ETag"] == f'"{data["version"]}"'


def test_put_saves_the_levels_and_the_window_and_returns_the_new_version(person):
    before = ok(person.client.get("/v1/autonomy"))["version"]
    response = put(
        person,
        {
            "actions": {"send_follow_up": {"level": "never"}, "twin_answer": {"level": "tell_me_after"}},
            "undo_window_minutes": 15,
        },
    )
    data = ok(response)
    assert data["actions"]["send_follow_up"]["level"] == "never" and data["undo_window_minutes"] == 15
    assert data["actions"]["twin_answer"]["level"] == "tell_me_after"
    assert data["version"] != before and response.headers["ETag"] == f'"{data["version"]}"'
    assert ok(person.client.get("/v1/autonomy")) == data
    assert stored(person.engine) == {"send_follow_up": "never", "twin_answer": "tell_me_after"}


@pytest.mark.parametrize("kind", ["send_follow_up", "submit_application"])
def test_tell_me_after_for_a_locked_type_is_refused_and_saves_nothing_including_the_valid_parts(person, kind):
    body = {
        "actions": {"twin_answer": {"level": "tell_me_after"}, kind: {"level": "tell_me_after"}},
        "undo_window_minutes": 20,
    }
    error = refused(put(person, body), 422, "autonomy_not_allowed")
    assert error["details"]["action_type"] == kind and error["details"]["requested_level"] == "tell_me_after"
    assert error["details"]["allowed_levels"] == (
        ["ask_first"] if kind == "submit_application" else ["ask_first", "never"]
    )
    assert stored(person.engine) == {}
    assert ok(person.client.get("/v1/autonomy"))["undo_window_minutes"] == 10


def test_a_type_the_dial_does_not_have_is_422_validation_failed(person):
    error = refused(put(person, {"actions": {"share_profile": {"level": "ask_first"}}}), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "share_profile"
    refused(put(person, {"actions": {"send_follow_up": {"level": "ask_first", "extra": 1}}}), 422, "validation_failed")
    refused(put(person, {"surprise": 1}), 422, "validation_failed")
    assert stored(person.engine) == {}


@pytest.mark.parametrize("minutes", [0, 61, -5])
def test_a_window_outside_one_to_sixty_is_refused(person, minutes):
    refused(put(person, {"undo_window_minutes": minutes}), 422, "validation_failed")
    assert ok(person.client.get("/v1/autonomy"))["undo_window_minutes"] == 10


@pytest.mark.parametrize("minutes", [1, 60])
def test_the_edges_of_the_window_are_saved(person, minutes):
    assert ok(put(person, {"undo_window_minutes": minutes}))["undo_window_minutes"] == minutes
    assert scalar(person.engine, "SELECT undo_window_minutes FROM users WHERE id = :u", u=person.user) == minutes


def test_a_stale_if_match_is_412_and_a_current_one_is_accepted(person):
    version = ok(person.client.get("/v1/autonomy"))["version"]
    ok(put(person, {"undo_window_minutes": 12}, if_match=f'"{version}"'))
    error = refused(put(person, {"undo_window_minutes": 14}, if_match=f'"{version}"'), 412, "version_conflict")
    assert error["details"]["current_version"] != version
    assert scalar(person.engine, "SELECT undo_window_minutes FROM users WHERE id = :u", u=person.user) == 12


def test_a_retry_with_the_same_key_returns_the_first_answer(person):
    headers = key()
    first = put(person, {"undo_window_minutes": 30}, headers=headers)
    again = put(person, {"undo_window_minutes": 30}, headers=headers)
    assert again.json()["data"] == first.json()["data"] and again.json()["meta"]["idempotent_replay"] is True
    assert again.headers["ETag"] == first.headers["ETag"]


def test_one_person_cannot_change_anothers_dial(person):
    put(person, {"actions": {"send_follow_up": {"level": "never"}}})
    sign_in(person.client, person.other)
    assert ok(person.client.get("/v1/autonomy"))["actions"]["send_follow_up"]["level"] == "ask_first"


def test_a_write_needs_an_idempotency_key(person):
    refused(person.client.put("/v1/autonomy", json={"undo_window_minutes": 5}), 400, "idempotency_key_missing")


def test_reading_and_changing_the_dial_need_only_the_api_login(person, monkeypatch):
    current = config.get()
    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
    monkeypatch.setattr(
        config, "get", lambda: replace(current, database_url_worker=unreachable, database_url_match=unreachable)
    )
    monkeypatch.setattr(db, "_engines", {})
    try:
        ok(person.client.get("/v1/autonomy"))
        ok(put(person, {"actions": {"send_follow_up": {"level": "never"}}}))
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
