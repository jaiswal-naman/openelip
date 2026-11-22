"""Idempotency keys with replay (PLT-03; API.md 1.6). Signed-in writes only: the public auth writes are exempt."""

from typing import Annotated, Any
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.responses import Response
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.platform import auth, outbox, sessions
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent, lock_name

KEY = "0f8fad5b-d9cb-469f-a165-70867728950e"


def make_thing(body: dict[str, Any], write: Annotated[Write, Depends(idempotent)]) -> Response:
    """The throwaway write: its effect is one outbox row, so a repeat that ran twice would show as two rows."""

    def effect(conn):
        outbox.add(conn, kind="test", dedupe_key=f"test:{uuid4()}", user_id=write.user_id, payload={})
        return {"made": body}

    return write.run(effect, status=201)


def refuse_thing(write: Annotated[Write, Depends(idempotent)]) -> Response:
    def effect(conn):
        outbox.add(conn, kind="test", dedupe_key=f"test:{uuid4()}", user_id=write.user_id, payload={})
        raise ApiError(404, "not_found", "No such thing.")

    return write.run(effect, status=201)


@pytest.fixture(autouse=True)
def fresh_address_limits():
    auth._recent.clear()


@pytest.fixture
def app(api_client):
    api_client.app.add_api_route("/v1/test/things", make_thing, methods=["POST"], status_code=201)
    api_client.app.add_api_route("/v1/test/refused", refuse_thing, methods=["POST"], status_code=201)
    return api_client.app


def signed_in(app, engine, email):
    client = TestClient(app)
    client.post("/v1/auth/codes", json={"email": email})
    with engine.connect() as c:
        sealed = c.execute(
            text(
                """SELECT o.payload->>'code_sealed' FROM outbox o
                   JOIN login_codes l ON l.id = CAST(o.payload->>'login_code_id' AS uuid)
                   WHERE l.email = :e ORDER BY o.created_at DESC LIMIT 1"""
            ),
            {"e": email},
        ).scalar_one()
    code = sessions.open_sealed(sealed)
    assert client.post("/v1/auth/sessions", json={"email": email, "code": code}).status_code == 201
    return client


def made(engine) -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM outbox WHERE template = 'test'")).scalar_one()


def post(client, body, key=KEY, path="/v1/test/things"):
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def test_a_signed_in_write_without_a_key_is_400_and_without_a_session_401(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    missing = client.post("/v1/test/things", json={})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "idempotency_key_missing"
    assert client.delete("/v1/auth/sessions/current").json()["error"]["code"] == "idempotency_key_missing"
    assert TestClient(app).post("/v1/test/things", json={}, headers={"Idempotency-Key": KEY}).status_code == 401
    assert made(engine) == 0


def test_the_public_auth_writes_need_no_key_and_store_none(api_client, engine):
    assert api_client.post("/v1/auth/codes", json={"email": "p@mail.test"}).status_code == 202
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0


def test_the_same_key_and_body_replays_the_first_response_and_changes_nothing(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    first = post(client, {"b": 2, "a": 1})
    second = post(client, {"a": 1, "b": 2})  # same JSON, other key order: the same request
    assert first.status_code == second.status_code == 201
    assert first.json()["data"] == second.json()["data"] == {"made": {"b": 2, "a": 1}}
    assert "idempotent_replay" not in first.json()["meta"] and second.json()["meta"]["idempotent_replay"] is True
    assert second.json()["meta"]["request_id"] == second.headers["x-request-id"]
    assert made(engine) == 1


def test_the_same_key_with_another_body_or_path_is_422(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    assert post(client, {"n": 1}).status_code == 201
    for changed in (post(client, {"n": 2}), post(client, {"n": 1}, path="/v1/test/refused")):
        assert changed.status_code == 422 and changed.json()["error"]["code"] == "idempotency_key_reused"
    assert made(engine) == 1


def test_keys_are_scoped_to_the_person(app, engine):
    ada, bob = signed_in(app, engine, "ada@mail.test"), signed_in(app, engine, "bob@mail.test")
    assert post(ada, {"n": 1}).status_code == post(bob, {"n": 2}).status_code == 201
    assert made(engine) == 2


def test_a_key_expires_after_24_hours(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    assert post(client, {"n": 1}).status_code == 201
    with engine.begin() as c:
        c.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '25 hours'"))
    again = post(client, {"n": 2})  # a different body is fine: the old key is gone
    assert again.status_code == 201 and "idempotent_replay" not in again.json()["meta"] and made(engine) == 2


def test_a_write_that_fails_records_nothing_so_a_retry_runs_again(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    for _ in range(2):
        assert post(client, {}, path="/v1/test/refused").status_code == 404
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM idempotency_keys")).scalar_one() == 0
    assert made(engine) == 0


def test_a_repeat_while_the_first_request_runs_is_409(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    with engine.connect() as c:
        user_id = c.execute(text("SELECT id FROM users")).scalar_one()
        c.execute(text("SELECT pg_advisory_lock(hashtextextended(:n, 0))"), {"n": lock_name(user_id, KEY)})
        busy = post(client, {})
        c.execute(text("SELECT pg_advisory_unlock_all()"))
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "request_in_progress" and made(engine) == 0
    assert post(client, {}).status_code == 201


def test_a_non_json_write_is_415_and_a_bad_key_is_400(app, engine):
    client = signed_in(app, engine, "a@mail.test")
    form = client.post(
        "/v1/test/things", content=b"a=1", headers={"Idempotency-Key": KEY, "content-type": "text/plain"}
    )
    assert form.status_code == 415 and form.json()["error"]["code"] == "unsupported_media_type"
    assert post(client, {}, key="short").json()["error"]["code"] == "invalid_request"
    assert post(client, {}, key=f'"{KEY}"').status_code == 201  # a quoted value is accepted, the quotes ignored
    assert post(client, {}).json()["meta"]["idempotent_replay"] is True
    assert made(engine) == 1
