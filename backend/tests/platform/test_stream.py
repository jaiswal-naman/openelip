"""GET /v1/control/stream (API 4.1 and 4.3): the endpoint and the source registry (plan 08-02).

Throwaway sources are registered here (the real ones are left out of the registry for the test and restored after), so
this file knows nothing about campaigns. Users are invented; the stream is read through the real app.
"""

import json
from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import text

from app.platform import db, sessions, stream
from app.platform.stream import Event, Limits
from tests.conftest import _empty_tables
from tests.match.factories import make_user


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


@pytest.fixture
def registry():
    """An empty registry for the test; the real one comes back afterwards."""
    saved = dict(stream._sources)
    stream._sources.clear()
    yield stream._sources
    stream._sources.clear()
    stream._sources.update(saved)


@pytest.fixture
def client(api_client):
    api_client.app.dependency_overrides[stream.limits] = lambda: Limits(2, 0, 3600)
    yield api_client
    api_client.app.dependency_overrides.pop(stream.limits, None)


def sign_in(client, engine) -> tuple[UUID, str]:
    with engine.begin() as conn:
        user = make_user(conn)
    with db.transaction(user) as conn:
        token = sessions.start(conn, user)
    client.cookies.set(sessions.COOKIE_NAME, token)
    return user, token


def read(client) -> str:
    with client.stream("GET", "/v1/control/stream") as response:
        assert response.status_code == 200
        return "".join(response.iter_text())


def frames(body: str) -> list[str]:
    return [f for f in body.split("\n\n") if f]


def test_signed_out_gets_the_401_envelope_and_no_stream(client, registry):
    response = client.get("/v1/control/stream")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "unauthenticated"
    client.cookies.set(sessions.COOKIE_NAME, "not-a-session")
    assert client.get("/v1/control/stream").status_code == 401


def test_headers_retry_snapshot_then_events_in_registration_order(client, clean, registry):
    user, _ = sign_in(client, clean)
    seen_users = []

    def snap_a(conn, uid):
        seen_users.append(uid)
        return {"a": 1}, 0

    def poll_a(conn, uid, state):
        return [Event("a.tick", {"n": state + 1})], state + 1

    stream.source("a", snapshot=snap_a, poll=poll_a)
    stream.source(
        "b", snapshot=lambda conn, uid: ({"b": [1, 2]}, None), poll=lambda c, u, s: ([Event("b.id", {}, id=7)], s)
    )
    with client.stream("GET", "/v1/control/stream") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        body = "".join(response.iter_text())
    assert body.startswith("retry: 3000\n\n")
    parts = frames(body)
    assert parts[1] == 'event: snapshot\ndata: {"a":1,"b":[1,2]}'
    assert parts[2:] == [
        'event: a.tick\ndata: {"n":1}',
        "id: 7\nevent: b.id\ndata: {}",
        'event: a.tick\ndata: {"n":2}',
        "id: 7\nevent: b.id\ndata: {}",
    ]
    assert seen_users == [user]


def test_a_ping_comment_is_written_when_nothing_was_sent(client, clean, registry):
    sign_in(client, clean)
    client.app.dependency_overrides[stream.limits] = lambda: Limits(2, 0, 0)
    stream.source("quiet", snapshot=lambda c, u: ({}, None), poll=lambda c, u, s: ([], s))
    assert read(client).count(": ping\n\n") == 2


def test_each_read_is_one_transaction_for_the_caller_off_the_event_loop(client, clean, registry):
    user, _ = sign_in(client, clean)
    seen = []

    def look(conn, uid, state=None):
        seen.append(conn.execute(text("SELECT current_setting('app.user_id'), current_user")).one())
        return {}, 1

    stream.source("who", snapshot=look, poll=lambda c, u, s: (look(c, u)[0] and [], s))
    read(client)
    assert seen and all(row == (str(user), "app_api") for row in seen)


def test_a_failing_poll_ends_the_stream_with_a_fixed_error_event(client, clean, registry, capsys):
    sign_in(client, clean)

    def boom(conn, uid, state):
        raise RuntimeError("secret row text 123")

    stream.source("flaky", snapshot=lambda c, u: ({"x": 1}, None), poll=boom)
    body = read(client)
    last = frames(body)[-1]
    assert last.startswith("event: error\n")
    assert json.loads(last.split("data: ")[1]) == {
        "code": "internal_error",
        "message": "The live stream stopped. Reconnecting.",
        "is_retryable": True,
    }
    assert "secret" not in body and "secret" not in capsys.readouterr().out
    assert frames(body).count(last) == 1


def test_a_failing_snapshot_is_an_error_event_too(client, clean, registry):
    sign_in(client, clean)

    def boom(conn, uid):
        raise RuntimeError("no")

    stream.source("flaky", snapshot=boom, poll=lambda c, u, s: ([], s))
    parts = frames(read(client))
    assert parts[0] == "retry: 3000" and parts[1].startswith("event: error")


def test_a_session_revoked_between_polls_ends_with_unauthenticated(client, clean, registry):
    _, token = sign_in(client, clean)
    client.app.dependency_overrides[stream.limits] = lambda: Limits(5, 0, 3600)

    def revoke_after_first(conn, uid, state):
        if state == 0:
            with db.transaction(uid) as other:
                sessions.revoke(other, token)
        return [Event("tick", {})], state + 1

    stream.source("s", snapshot=lambda c, u: ({}, 0), poll=revoke_after_first)
    parts = frames(read(client))
    assert [p.split("\n")[0] for p in parts] == ["retry: 3000", "event: snapshot", "event: tick", "event: error"]
    assert json.loads(parts[-1].split("data: ")[1]) == {
        "code": "unauthenticated",
        "message": "Sign in to continue.",
        "is_retryable": False,
    }


def test_a_name_registered_twice_raises(registry):
    stream.source("one", snapshot=lambda c, u: ({}, None), poll=lambda c, u, s: ([], s))
    with pytest.raises(RuntimeError):
        stream.source("one", snapshot=lambda c, u: ({}, None), poll=lambda c, u, s: ([], s))


def test_stored_text_cannot_start_a_new_field():
    hostile = "x\nevent: error\n\ndata: {} "
    out = stream.frame(
        Event("recorder.step", {"summary": hostile, "at": datetime(2026, 11, 23, tzinfo=timezone.utc)}, id=5)
    )
    assert out.count("\n") == 4 and out.endswith("\n\n")
    assert json.loads(out.split("data: ", 1)[1])["summary"] == hostile


def test_the_stream_runs_with_the_worker_and_match_logins_unreachable(client, clean, registry, monkeypatch):
    """The API process holds only the API login: no source may open another one (see test_publish_api_login.py)."""
    from dataclasses import replace

    from app.platform import config

    user, _ = sign_in(client, clean)
    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
    changed = replace(config.get(), database_url_worker=unreachable, database_url_match=unreachable)
    monkeypatch.setattr(config, "get", lambda: changed)
    saved = dict(db._engines)
    monkeypatch.setattr(db, "_engines", {})
    try:
        import app.campaigns.stream as campaigns_stream
        import app.guardian.stream as guardian_stream
        import app.hiring.stream as hiring_stream

        for name, module in (("campaigns", campaigns_stream), ("guardian", guardian_stream), ("hiring", hiring_stream)):
            stream.source(name, snapshot=module.snapshot, poll=module.poll)
        body = read(client)
        assert "event: error" not in body and '"campaigns":[]' in body
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
        monkeypatch.undo()
        db._engines.update(saved)
