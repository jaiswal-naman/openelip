"""Sign in with an emailed code: codes, sessions, limits and the cookie (PLT-02; ARCHITECTURE 6.1 and 6.2)."""

import json
import logging
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.requests import Request

from app.platform import auth, config, sessions

EMAIL = "Ada.Lovelace@Mail.Test"  # mixed case on purpose: stored and looked up lower-case
NEW_CODE = "482913"


@pytest.fixture(autouse=True)
def fresh_address_limits():
    auth._recent.clear()  # the per-address counter lives in process memory; every test starts with an empty one


def last_code(engine, email=EMAIL) -> str:
    with engine.connect() as c:
        sealed = c.execute(
            text(
                """SELECT o.payload->>'code_sealed' FROM outbox o
                   JOIN login_codes l ON l.id = CAST(o.payload->>'login_code_id' AS uuid)
                   WHERE lower(l.email) = :e ORDER BY o.created_at DESC LIMIT 1"""
            ),
            {"e": email.lower()},
        ).scalar_one()
    return sessions.open_sealed(sealed)


def ask(client, email=EMAIL):
    return client.post("/v1/auth/codes", json={"email": email})


def sign_in(client, engine, email=EMAIL, **extra):
    assert ask(client, email).status_code == 202
    return client.post("/v1/auth/sessions", json={"email": email, "code": last_code(engine, email), **extra})


def rows(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).all()


def test_a_code_request_stores_a_keyed_hmac_and_queues_exactly_one_email(api_client, engine):
    response = ask(api_client)
    assert response.status_code == 202 and response.json()["data"] == {"expires_in_seconds": 600}
    (login,) = rows(engine, "SELECT id, email, code_hmac, attempts FROM login_codes")
    assert login.email == EMAIL.lower() and len(login.code_hmac) == 32 and login.attempts == 0
    (box,) = rows(engine, "SELECT dedupe_key, template, status FROM outbox")
    assert (box.dedupe_key, box.template, box.status) == (f"signin:{login.id}", "signin", "pending")
    digits = last_code(engine)
    assert login.code_hmac == auth._code_hmac(EMAIL.lower(), digits)  # keyed: it is not a bare hash of the digits


def test_the_answer_is_the_same_for_a_new_and_a_known_address(api_client, engine):
    first = ask(api_client, "new@mail.test")
    assert sign_in(api_client, engine, "known@mail.test").status_code == 201
    second = ask(api_client, "known@mail.test")
    assert (first.status_code, first.json()["data"]) == (second.status_code, second.json()["data"])


def test_the_sixth_code_for_one_email_in_an_hour_is_refused(api_client):
    assert [ask(api_client).status_code for _ in range(5)] == [202] * 5
    sixth = ask(api_client, EMAIL.upper())  # the same address in another spelling
    assert sixth.status_code == 429 and sixth.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(sixth.headers["retry-after"]) <= 3600


def test_the_twenty_first_request_from_one_address_is_refused_and_another_address_is_not(api_client):
    codes = [ask(api_client, f"person{i}@mail.test").status_code for i in range(21)]
    assert codes == [202] * 20 + [429]
    other = TestClient(api_client.app, client=("203.0.113.9", 1234))
    assert ask(other, "someone@mail.test").status_code == 202


def test_an_ipv6_host_is_counted_by_its_slash_64_and_an_ipv4_mapped_address_as_ipv4():
    def key(host):
        return auth._client_key(Request({"type": "http", "client": (host, 1), "headers": []}))

    assert key("2001:db8::1") == key("2001:db8::2")
    assert key("2001:db8:0:1::1") != key("2001:db8::1")
    assert key("::ffff:192.0.2.7") == "192.0.2.7" and key("192.0.2.7") == "192.0.2.7"


def test_a_wrong_code_counts_an_attempt_and_five_attempts_lock_the_code(api_client, engine):
    ask(api_client)
    right = last_code(engine)
    wrong = "000000" if right != "000000" else "111111"
    for tried in range(1, 6):
        r = api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": wrong})
        assert r.status_code == 401 and r.json()["error"]["code"] == "code_invalid"
        assert r.json()["error"]["details"]["attempts_remaining"] == 5 - tried
        assert rows(engine, "SELECT attempts FROM login_codes")[0].attempts == tried  # kept although the request failed
    late = api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": right})
    assert late.status_code == 401 and rows(engine, "SELECT count(*) AS n FROM sessions")[0].n == 0


def test_a_used_an_expired_and_a_replaced_code_all_fail(api_client, engine):
    assert sign_in(api_client, engine).status_code == 201
    used = last_code(engine)
    assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": used}).status_code == 401
    ask(api_client)
    old = last_code(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE login_codes SET expires_at = now() - interval '1 second' WHERE consumed_at IS NULL"))
    assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": old}).status_code == 401
    ask(api_client)
    first = last_code(engine)
    ask(api_client)  # a new code replaces the one before it
    second = last_code(engine)
    if first != second:
        assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": first}).status_code == 401
    assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": second}).status_code == 201


def test_the_right_code_signs_in_with_a_hardened_cookie_and_stores_only_hashes(api_client, engine):
    response = sign_in(api_client, engine, timezone="Asia/Kolkata")
    assert response.status_code == 201
    me = response.json()["data"]
    assert me["email"] == EMAIL.lower() and me["timezone"] == "Asia/Kolkata"
    cookie = response.headers["set-cookie"]
    assert re.search(r"app_session=[\w-]{40,}", cookie)
    for flag in ("httponly", "samesite=lax", "path=/", "max-age=7776000"):
        assert flag in cookie.lower()
    assert "secure" not in cookie.lower()  # APP_BASE_URL is plain http here
    token = api_client.cookies.get("app_session")
    (stored,) = rows(engine, "SELECT token_sha256, user_id FROM sessions")
    assert stored.token_sha256 == sessions.token_hash(token) and token.encode() not in bytes(stored.token_sha256)
    sign_in(api_client, engine, timezone="Europe/Paris")  # a second sign-in: same account, timezone not rewritten
    assert [r.timezone for r in rows(engine, "SELECT timezone FROM users")] == ["Asia/Kolkata"]


def test_an_unknown_timezone_is_refused_before_the_code_is_spent(api_client, engine):
    ask(api_client)
    body = {"email": EMAIL, "code": last_code(engine), "timezone": "Mars/Olympus"}
    r = api_client.post("/v1/auth/sessions", json=body)
    assert r.status_code == 422 and r.json()["error"]["details"]["fields"][0]["field"] == "timezone"
    assert api_client.post("/v1/auth/sessions", json={**body, "timezone": None}).status_code == 201


def test_the_cookie_is_secure_when_the_public_address_is_https(monkeypatch):
    https = replace(config.get(), app_base_url="https://careers.example.org")
    monkeypatch.setattr(config, "get", lambda: https)
    assert sessions.cookie_options()["secure"] is True


def test_me_works_with_the_cookie_on_a_fresh_client_and_not_without_it(api_client, engine):
    assert api_client.get("/v1/me").status_code == 401
    sign_in(api_client, engine)
    assert api_client.get("/v1/me").json()["data"]["email"] == EMAIL.lower()
    refreshed = TestClient(api_client.app, cookies={"app_session": api_client.cookies.get("app_session")})
    assert refreshed.get("/v1/me").status_code == 200
    assert TestClient(api_client.app, cookies={"app_session": "not-a-session"}).get("/v1/me").status_code == 401


def test_sign_out_revokes_the_session_and_clears_the_cookie(api_client, engine):
    sign_in(api_client, engine)
    token = api_client.cookies.get("app_session")
    gone = api_client.delete("/v1/auth/sessions/current", headers={"Idempotency-Key": "sign-out-0001"})
    assert gone.status_code == 204 and "max-age=0" in gone.headers["set-cookie"].lower()
    assert api_client.cookies.get("app_session") is None
    assert rows(engine, "SELECT revoked_at FROM sessions")[0].revoked_at is not None
    assert TestClient(api_client.app, cookies={"app_session": token}).get("/v1/me").status_code == 401


@pytest.mark.parametrize(
    "age",
    [
        "last_seen_at = now() - interval '31 days'",
        "created_at = now() - interval '100 days', expires_at = now() - interval '10 days'",
    ],
    ids=["idle 30 days", "older than 90 days"],
)
def test_an_idle_or_too_old_session_is_refused(api_client, engine, age):
    sign_in(api_client, engine)
    assert api_client.get("/v1/me").status_code == 200
    with engine.begin() as c:
        c.execute(text(f"UPDATE sessions SET {age}"))
    assert api_client.get("/v1/me").status_code == 401


def test_a_suspended_account_has_no_session(api_client, engine):
    sign_in(api_client, engine)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET status = 'suspended'"))
    assert api_client.get("/v1/me").status_code == 401
    assert sign_in(api_client, engine).status_code == 403


def test_last_seen_is_touched_at_most_once_a_minute(api_client, engine):
    sign_in(api_client, engine)
    before = rows(engine, "SELECT last_seen_at FROM sessions")[0].last_seen_at
    api_client.get("/v1/me")
    assert rows(engine, "SELECT last_seen_at FROM sessions")[0].last_seen_at == before
    with engine.begin() as c:
        c.execute(text("UPDATE sessions SET last_seen_at = now() - interval '2 minutes'"))
    api_client.get("/v1/me")
    assert rows(engine, "SELECT now() - last_seen_at < interval '1 minute' AS fresh FROM sessions")[0].fresh


def test_no_plaintext_code_is_in_the_database_and_nothing_secret_is_logged(api_client, engine, monkeypatch, caplog):
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: int(NEW_CODE))
    caplog.set_level(logging.INFO, logger="app.event")
    assert sign_in(api_client, engine).status_code == 201
    assert last_code(engine) == NEW_CODE  # the sealed value does decrypt to it
    dump = " ".join(
        json.dumps(r[0], default=str)
        for table in ("login_codes", "outbox", "sessions", "users", "idempotency_keys")
        for r in rows(engine, f"SELECT row_to_json(t) FROM {table} t")
    )
    assert not re.search(rf"(?<![\w.:+-]){NEW_CODE}(?![\w.:+-])", dump)
    logged = caplog.text
    assert NEW_CODE not in logged and "lovelace" not in logged.lower() and "mail.test" not in logged.lower()
    for name in ("auth.code_requested", "auth.signed_in"):
        assert name in logged


def test_a_non_json_write_is_415_and_no_cors_headers_are_sent(api_client):
    r = api_client.post("/v1/auth/codes", content=b"email=a@b.c", headers={"content-type": "text/plain"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"
    r = api_client.post("/v1/auth/codes", json={"email": EMAIL}, headers={"Origin": "https://evil.example"})
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]
