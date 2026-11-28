"""Local mode: sign-in with no code, the Host guard, the model key saved from the app (APP_MODE=local)."""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.platform import config, db, relay, sessions
from app.platform.llm import AIDisabled, registry, stored_key

KEY = "sk-ant-api03-0123456789abcdefLAST"
OTHER_KEY = "sk-proj-zyxwvutsrqponmlkjihgFINE"
HOME = "http://localhost:8080"
OWNER = "owner@example.test"


def settings(**changes) -> config.Settings:
    """Local mode on a settings object that has nothing about a model, whatever the developer's shell holds."""
    base = dict(
        app_mode="local",
        llm_provider="anthropic",
        llm_provider_is_set=False,
        anthropic_api_key=None,
        openai_api_key=None,
        llm_base_url=None,
    )
    return replace(config.get(), **{**base, **changes})


@pytest.fixture(autouse=True)
def fresh_key(engine):
    """No saved key and no cached one at the start and the end of every test (not every test has an api_client)."""
    with engine.begin() as c:
        c.execute(text("TRUNCATE local_model_key"))
    stored_key.invalidate()
    yield
    with engine.begin() as c:
        c.execute(text("TRUNCATE local_model_key"))
    stored_key.invalidate()


@pytest.fixture
def use(monkeypatch):
    """use(**changes): run this test with local-mode settings (or, with app_mode="server", with the old ones)."""

    def install(**changes) -> config.Settings:
        chosen = settings(**changes)
        monkeypatch.setattr(config, "get", lambda: chosen)
        return chosen

    return install


@pytest.fixture
def local(api_client, use):
    """A browser on the owner's own computer, with the process in local mode."""
    use()
    return TestClient(api_client.app, base_url=HOME)


def write(client, method, path, **kwargs):
    headers = {"Idempotency-Key": str(uuid.uuid4()), **kwargs.pop("headers", {})}
    return client.request(method, path, headers=headers, **kwargs)


def sign_in(client):
    response = client.post("/v1/auth/local", json={})
    assert response.status_code == 201
    return response.json()["data"]


def rows(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def real_account(engine, email="ada@mail.test", status="active") -> uuid.UUID:
    with engine.begin() as c:
        return c.execute(
            text("INSERT INTO users (email, status) VALUES (:e, CAST(:s AS user_status)) RETURNING id"),
            {"e": email, "s": status},
        ).scalar_one()


def session_for(app, engine, user) -> TestClient:
    client = TestClient(app, base_url=HOME)
    with engine.begin() as c:
        client.cookies.set(sessions.COOKIE_NAME, sessions.start(c, user))
    return client


# --- the mode, and the Host guard ------------------------------------------------------------------------------------

ROUTES = [
    ("post", "/v1/auth/local", {"json": {}}),
    ("get", "/v1/local/status", {}),
    ("put", "/v1/settings/model-key", {"json": {"provider": "anthropic", "api_key": KEY}}),
    ("delete", "/v1/settings/model-key", {}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES)
def test_server_mode_has_none_of_these_routes(api_client, use, method, path, kwargs):
    use(app_mode="server")
    client = TestClient(api_client.app, base_url=HOME)  # even addressed to localhost
    response = write(client, method, path, **kwargs)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES)
@pytest.mark.parametrize(
    "host",
    [
        "localhost.evil.com",  # starts like localhost
        "127.0.0.1.nip.io",  # starts like the loopback address
        "evil.com",
        "evil.com:8080",
        "localhost.",  # a trailing dot is another name
        "localhost@evil.com",
        "localhost:8080@evil.com",
        "evil.com#localhost",
        "localhost:99999x",
        "0.0.0.0",
        "127.0.0.2",
        "[::2]",
        "::1",  # an IPv6 address in a Host header is bracketed
        "",
    ],
)
def test_a_host_that_is_not_this_computer_is_refused_on_every_route(local, method, path, kwargs, host):
    response = write(local, method, path, headers={"Host": host}, **kwargs)
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8080", "LOCALHOST:3000", "127.0.0.1", "127.0.0.1:8080", "[::1]", "[::1]:8080"]
)
def test_every_name_a_browser_uses_for_this_computer_is_accepted(local, host):
    assert local.get("/v1/local/status", headers={"Host": host}).status_code == 200
    assert local.post("/v1/auth/local", json={}, headers={"Host": host}).status_code == 201


def test_two_host_headers_are_refused_even_when_one_is_localhost(local):
    headers = [("Host", "localhost:8080"), ("Host", "evil.com")]
    assert local.get("/v1/local/status", headers=headers).status_code == 403


def test_local_mode_is_refused_off_localhost():
    public = {"APP_MODE": "local", "APP_BASE_URL": "https://careers.example.org"}
    with pytest.raises(config.ConfigError, match="APP_MODE=local"):
        config.load(public)


def test_local_mode_has_no_emailed_sign_in_so_no_page_can_make_a_second_account(local, engine):
    assert local.post("/v1/auth/codes", json={"email": "a@mail.test"}).status_code == 404
    assert local.post("/v1/auth/sessions", json={"email": "a@mail.test", "code": "123456"}).status_code == 404
    assert (
        rows(engine, "SELECT count(*) FROM login_codes")[0][0] == 0
        and rows(engine, "SELECT count(*) FROM users")[0][0] == 0
    )


def test_server_mode_keeps_the_emailed_sign_in(api_client, use):
    use(app_mode="server")
    assert api_client.post("/v1/auth/codes", json={"email": "a@mail.test"}).status_code == 202


# --- signing the owner in --------------------------------------------------------------------------------------------


def test_the_first_call_makes_the_owner_and_the_second_uses_the_same_account(local, engine):
    first = local.post("/v1/auth/local", json={})
    assert first.status_code == 201
    me = first.json()["data"]
    assert me["email"] == OWNER and set(me) >= {"id", "timezone", "consents", "brief", "capabilities", "organizations"}
    cookie = first.headers["set-cookie"].lower()
    assert "app_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert local.get("/v1/me").json()["data"]["id"] == me["id"]  # the cookie is the session

    second = TestClient(local.app, base_url=HOME).post("/v1/auth/local", json={})
    assert second.status_code == 201 and second.json()["data"]["id"] == me["id"]
    assert rows(engine, "SELECT count(*) FROM users")[0][0] == 1
    assert rows(engine, "SELECT count(*) FROM sessions")[0][0] == 2


def test_the_owner_has_agreed_to_ai_processing_from_the_first_sign_in_with_no_click(local, engine):
    me = sign_in(local)
    ai = next(c for c in me["consents"] if c["purpose"] == "ai_processing")
    assert ai["granted"] is True and ai["text_version"] == ai["current_text_version"] is not None
    (grant,) = rows(engine, "SELECT action::text AS action, text_version FROM consents WHERE user_id = :u", u=me["id"])
    assert (grant.action, grant.text_version) == ("granted", ai["current_text_version"])
    (audit,) = rows(
        engine, "SELECT event_type, details->>'source' AS source FROM audit_log WHERE subject_user_id = :u", u=me["id"]
    )
    assert (audit.event_type, audit.source) == ("consent.granted", "local_setup")


def test_the_owner_is_a_real_account_that_no_mail_is_ever_sent_to(local, engine):
    sign_in(local)
    (owner,) = rows(engine, "SELECT email, is_synthetic, status::text AS status FROM users")
    assert (owner.email, owner.is_synthetic, owner.status) == (OWNER, False, "active")  # not excluded from any feature
    with engine.connect() as c:
        assert relay.is_undeliverable(c, owner.email)  # example.test is reserved: the relay cancels the message


def test_two_first_calls_at_once_make_one_owner(local, engine):
    def call(_):
        return TestClient(local.app, base_url=HOME).post("/v1/auth/local", json={}).json()["data"]["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = set(pool.map(call, range(4)))
    assert len(ids) == 1 and rows(engine, "SELECT count(*) FROM users")[0][0] == 1


def test_a_synthetic_account_is_not_a_second_account(local, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (email, is_synthetic) VALUES ('demo@example.test', true)"))
    assert local.post("/v1/auth/local", json={}).json()["data"]["email"] == OWNER


def test_one_account_from_server_mode_is_the_owner_and_no_second_is_made(local, engine):
    mine = real_account(engine)
    assert sign_in(local)["id"] == str(mine)
    assert rows(engine, "SELECT count(*) FROM users")[0][0] == 1


def test_more_than_one_real_account_is_refused_and_nothing_is_created(local, engine):
    real_account(engine, "ada@mail.test")
    real_account(engine, "grace@mail.test")
    response = local.post("/v1/auth/local", json={})
    error = response.json()["error"]
    assert (
        response.status_code == 409
        and error["code"] == "conflict"
        and error["details"] == {"state": "several_accounts"}
    )
    assert "server mode" in error["message"]
    assert "set-cookie" not in response.headers
    assert rows(engine, "SELECT count(*) FROM sessions")[0][0] == 0
    assert rows(engine, "SELECT count(*) FROM users")[0][0] == 2
    assert (
        local.get("/v1/local/status").json()["data"]["has_owner"] is True
    )  # accounts exist; signing in is what is refused


def test_a_suspended_owner_cannot_sign_in(local, engine):
    real_account(engine, status="suspended")
    assert local.post("/v1/auth/local", json={}).status_code == 403


def test_the_body_must_be_json(local):
    assert local.post("/v1/auth/local").status_code == 422  # a body is required, so a form that cannot send one fails
    refused = local.post("/v1/auth/local", content=b"{}", headers={"content-type": "text/plain"})
    assert refused.status_code == 415


def test_the_time_zone_is_kept_only_when_the_call_makes_the_account(local):
    assert local.post("/v1/auth/local", json={"timezone": "Asia/Kolkata"}).json()["data"]["timezone"] == "Asia/Kolkata"
    again = TestClient(local.app, base_url=HOME).post("/v1/auth/local", json={"timezone": "America/Chicago"})
    assert again.json()["data"]["timezone"] == "Asia/Kolkata"
    assert local.post("/v1/auth/local", json={"timezone": "Mars/Olympus"}).status_code == 422


def test_the_owner_is_a_candidate_and_an_employer_at_once(local, engine):
    """Job seekers and employers have equal weight in the local app: nothing on the account stops the one owner from
    creating an organization and using the hiring desk beside their own record."""
    owner = sign_in(local)
    org = write(local, "post", "/v1/orgs", json={"name": "Halden Studio", "timezone": "Europe/Berlin"})
    assert org.status_code == 201
    org_id = org.json()["data"]["id"]
    assert org.json()["data"]["role"] == "owner" and org.json()["data"]["is_synthetic"] is False

    memberships = local.get("/v1/me").json()["data"]["organizations"]
    assert [(m["id"], m["role"]) for m in memberships] == [(org_id, "owner")]

    role = write(
        local,
        "post",
        "/v1/roles",
        json={"org_id": org_id, "title": "Backend engineer", "description": "Build the thing."},
    )
    assert role.status_code == 201, role.text
    assert [r["title"] for r in local.get("/v1/roles", params={"org_id": org_id}).json()["data"]] == [
        "Backend engineer"
    ]
    assert local.get(f"/v1/orgs/{org_id}").json()["data"]["members"][0]["user_id"] == owner["id"]
    assert rows(engine, "SELECT count(*) FROM users")[0][0] == 1  # still the one account on both sides


# --- what the web asks before it shows anything ----------------------------------------------------------------------


def test_status_needs_no_session_and_says_whether_there_is_an_owner(local):
    first = local.get("/v1/local/status").json()["data"]
    assert first["mode"] == "local" and first["has_owner"] is False
    assert first["model"] == {"provider": "anthropic", "key_set": False, "key_last4": None, "source": None}
    sign_in(local)
    assert local.get("/v1/local/status").json()["data"]["has_owner"] is True


def test_status_names_the_fake_provider_so_the_setup_page_can_offer_demo_data(local, use):
    use(llm_provider="fake", llm_provider_is_set=True)
    assert local.get("/v1/local/status").json()["data"]["model"] == {
        "provider": "fake",
        "key_set": False,
        "key_last4": None,
        "source": None,
    }


def test_a_model_server_you_run_needs_no_key(local, use):
    use(llm_provider="openai_compatible", llm_provider_is_set=True, llm_base_url="http://llm:11434/v1")
    model = local.get("/v1/local/status").json()["data"]["model"]
    assert (model["provider"], model["key_set"], model["key_last4"]) == ("openai_compatible", True, None)


# --- the model key ---------------------------------------------------------------------------------------------------


def save(client, provider="anthropic", api_key=KEY):
    return write(client, "put", "/v1/settings/model-key", json={"provider": provider, "api_key": api_key})


def test_the_key_routes_need_a_session_and_the_usual_write_headers(local):
    assert save(local).status_code == 401
    assert write(local, "delete", "/v1/settings/model-key").status_code == 401
    sign_in(local)
    no_key = local.put("/v1/settings/model-key", json={"provider": "anthropic", "api_key": KEY})
    assert no_key.status_code == 400 and no_key.json()["error"]["code"] == "idempotency_key_missing"


@pytest.mark.parametrize(
    "bad", ["", "short", "has a space-in-the-middle-0123", "tab\tinside-0123456789", "café-0123456789", "x" * 513]
)
def test_a_key_that_cannot_be_a_provider_key_is_refused_without_echoing_it(local, bad):
    sign_in(local)
    response = save(local, api_key=bad)
    assert response.status_code == 422 and response.json()["error"]["details"]["fields"][0]["field"] == "api_key"
    assert bad not in response.text or bad == ""


def test_an_unknown_provider_is_refused(local):
    sign_in(local)
    assert save(local, provider="fake").status_code == 422


def test_the_key_is_stored_sealed_and_comes_back_only_as_its_last_four_characters(local, engine, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    sign_in(local)
    padded = save(local, api_key=f"  {KEY}\n")  # a pasted newline and spaces are dropped
    assert padded.status_code == 200
    assert padded.json()["data"] == {"provider": "anthropic", "key_set": True, "key_last4": "LAST", "source": "app"}

    (row,) = rows(engine, "SELECT provider, api_key_sealed, key_last4 FROM local_model_key")
    assert row.provider == "anthropic" and row.key_last4 == "LAST"
    assert (
        KEY not in row.api_key_sealed and sessions.open_sealed(row.api_key_sealed) == KEY
    )  # a Fernet token, not the key

    everything = rows(
        engine,
        """SELECT coalesce(string_agg(row_to_json(l)::text, ' '), '') FROM local_model_key l
           UNION ALL SELECT coalesce(string_agg(row_to_json(i)::text, ' '), '') FROM idempotency_keys i
           UNION ALL SELECT coalesce(string_agg(row_to_json(s)::text, ' '), '') FROM sessions s""",
    )
    stored = " ".join(r[0] for r in everything)
    assert (
        "LAST" in stored and KEY not in stored and "0123456789abcdef" not in stored.replace("0123456789abcdefLAST", "")
    )

    status = local.get("/v1/local/status")
    assert status.json()["data"]["model"] == {
        "provider": "anthropic",
        "key_set": True,
        "key_last4": "LAST",
        "source": "app",
    }
    assert KEY not in status.text and KEY not in local.get("/v1/me").text
    assert "settings.model_key_saved" in caplog.text and KEY not in caplog.text  # the log lines never carry it


def test_saving_again_replaces_the_one_row_and_deleting_removes_it(local, engine):
    sign_in(local)
    save(local)
    assert save(local, provider="openai", api_key=OTHER_KEY).json()["data"]["key_last4"] == "FINE"
    assert [(r.provider, r.key_last4) for r in rows(engine, "SELECT provider, key_last4 FROM local_model_key")] == [
        ("openai", "FINE")
    ]
    assert write(local, "delete", "/v1/settings/model-key").status_code == 204
    assert rows(engine, "SELECT count(*) FROM local_model_key")[0][0] == 0
    assert write(local, "delete", "/v1/settings/model-key").status_code == 204  # nothing to remove is not an error
    assert local.get("/v1/local/status").json()["data"]["model"]["key_set"] is False


def test_a_repeated_idempotency_key_is_replayed_without_the_key_in_the_answer(local):
    sign_in(local)
    headers = {"Idempotency-Key": "same-key-for-both-calls"}
    body = {"provider": "anthropic", "api_key": KEY}
    first = local.put("/v1/settings/model-key", json=body, headers=headers)
    again = local.put("/v1/settings/model-key", json=body, headers=headers)
    assert (first.status_code, again.status_code) == (200, 200) and again.json()["meta"]["idempotent_replay"] is True
    assert KEY not in again.text


@pytest.mark.parametrize(
    ("env", "provider"),
    [
        ({"llm_provider": "fake", "llm_provider_is_set": True}, "anthropic"),  # LLM_PROVIDER names another provider
        ({"anthropic_api_key": "sk-from-the-environment"}, "anthropic"),  # a key is already there
        ({"anthropic_api_key": "sk-from-the-environment"}, "openai"),  # a key for another provider names the model
    ],
)
def test_a_key_the_environment_would_override_is_a_conflict_not_a_silent_no_op(local, engine, use, env, provider):
    sign_in(local)
    use(**env)
    response = save(local, provider=provider)
    assert response.status_code == 409 and response.json()["error"]["details"] == {"state": "set_in_environment"}
    assert rows(engine, "SELECT count(*) FROM local_model_key")[0][0] == 0


def test_only_the_owner_may_change_the_key(local, engine):
    real_account(engine, "ada@mail.test")
    other = session_for(local.app, engine, real_account(engine, "grace@mail.test"))  # two accounts: nobody is the owner
    assert save(other).status_code == 403
    assert write(other, "delete", "/v1/settings/model-key").status_code == 403


# --- the roles -------------------------------------------------------------------------------------------------------


def test_the_api_writes_only_for_a_signed_in_person_and_the_worker_only_reads(local, engine):
    insert = text("INSERT INTO local_model_key (provider, api_key_sealed, key_last4) VALUES ('openai', 'x', 'abcd')")
    update, delete = text("UPDATE local_model_key SET provider = 'openai'"), text("DELETE FROM local_model_key")
    owner = uuid.UUID(sign_in(local)["id"])
    with pytest.raises(DBAPIError, match="row-level security"):  # the API's own login, in a request nobody signed in to
        with db.transaction(role="api") as conn:
            conn.execute(insert)
    with db.transaction(owner, role="api") as conn:  # the same login for a signed-in person
        conn.execute(insert)
    with db.transaction(role="api") as conn:  # it may read, but its writes touch no row
        assert conn.execute(text("SELECT count(*) FROM local_model_key")).scalar_one() == 1
        assert conn.execute(update).rowcount == 0 and conn.execute(delete).rowcount == 0
    with db.transaction(role="worker") as conn:
        assert conn.execute(text("SELECT provider FROM local_model_key")).scalar_one() == "openai"
    for statement in (insert, update, delete):
        with pytest.raises(DBAPIError, match="permission denied"):
            with db.transaction(role="worker") as conn:
                conn.execute(statement)
    with pytest.raises(DBAPIError, match="permission denied"):  # the match login has no use for it
        with db.transaction(role="match") as conn:
            conn.execute(text("SELECT * FROM local_model_key"))
    assert rows(engine, "SELECT provider FROM local_model_key") == [("openai",)]


def test_the_worker_reads_the_key_the_api_saved(local, monkeypatch):
    sign_in(local)
    save(local)
    monkeypatch.setattr(stored_key, "_login", "worker")
    stored_key.invalidate()
    assert stored_key.current() == stored_key.StoredKey("anthropic", KEY)


def test_only_the_api_login_can_ask_who_the_owner_is(engine):
    allowed = {
        role: rows(engine, "SELECT has_function_privilege(:r, 'local_owner(boolean)', 'EXECUTE')", r=role)[0][0]
        for role in ("app_api", "app_worker", "app_match")
    }
    assert allowed == {"app_api": True, "app_worker": False, "app_match": False}


# --- model calls use the saved key -----------------------------------------------------------------------------------


def seal_into(engine, provider, key, sealing=sessions.seal):
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO local_model_key (provider, api_key_sealed, key_last4) VALUES (:p, :s, :l)"),
            {"p": provider, "s": sealing(key), "l": key[-4:]},
        )


def test_a_model_call_uses_the_saved_key_and_provider_when_the_environment_says_nothing(engine):
    seal_into(engine, "openai", OTHER_KEY)
    route = registry.route("ping", settings())
    assert (route.provider, route.adapter, route.api_key) == ("openai", "openai_compatible", OTHER_KEY)
    assert (
        registry.route("ping", settings(app_mode="server", anthropic_api_key="env")).api_key == "env"
    )  # server mode: untouched
    with pytest.raises(AIDisabled):  # and in server mode the saved row is not looked at at all
        registry.route("ping", settings(app_mode="server"))


def test_the_environment_wins_over_the_saved_key(engine):
    seal_into(engine, "openai", OTHER_KEY)
    env_key = registry.route("ping", settings(anthropic_api_key="sk-from-the-environment"))
    assert (env_key.provider, env_key.api_key) == ("anthropic", "sk-from-the-environment")
    pinned = registry.route("ping", settings(llm_provider="fake", llm_provider_is_set=True))
    assert pinned.provider == "fake"
    with pytest.raises(AIDisabled):  # LLM_PROVIDER names a provider the saved key is not for: no key, no call
        registry.route("ping", settings(llm_provider="anthropic", llm_provider_is_set=True))


def test_the_saved_key_fills_in_a_provider_the_environment_pinned_without_a_key(engine):
    seal_into(engine, "anthropic", KEY)
    route = registry.route("ping", settings(llm_provider="anthropic", llm_provider_is_set=True))
    assert (route.provider, route.api_key) == ("anthropic", KEY)


def test_a_saved_openai_key_is_never_sent_to_a_model_server_you_run(engine):
    seal_into(engine, "openai", OTHER_KEY)
    route = registry.route(
        "ping", settings(llm_provider="openai_compatible", llm_provider_is_set=True, llm_base_url="http://llm:11434/v1")
    )
    assert route.api_key == registry.NO_KEY


def test_the_row_is_read_once_in_a_while_not_on_every_call(engine, monkeypatch):
    seal_into(engine, "anthropic", KEY)
    reads = []
    real = stored_key._read
    monkeypatch.setattr(stored_key, "_read", lambda: reads.append(1) or real())
    for _ in range(5):
        assert registry.route("ping", settings()).api_key == KEY
    assert len(reads) == 1
    monkeypatch.setattr(stored_key, "CACHE_SECONDS", 0.0)
    registry.route("ping", settings())
    registry.route("ping", settings())
    assert len(reads) == 3


def test_a_saved_key_that_cannot_be_opened_is_the_same_as_no_key(engine, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    other_data_key = Fernet.generate_key().decode()
    seal_into(engine, "anthropic", KEY, sealing=lambda key: Fernet(other_data_key).encrypt(key.encode()).decode())
    with pytest.raises(AIDisabled) as refused:
        registry.route("ping", settings())
    assert refused.value.reason == "no_key"
    assert "model_key.unreadable" in caplog.text and KEY not in caplog.text


def test_the_key_is_changed_in_the_cache_as_soon_as_this_process_saves_or_deletes_it(local):
    sign_in(local)
    assert stored_key.current() is None
    save(local)
    assert stored_key.current() == stored_key.StoredKey("anthropic", KEY)
    write(local, "delete", "/v1/settings/model-key")
    assert stored_key.current() is None


def test_no_stored_key_means_the_old_behaviour(engine):
    assert stored_key.apply(settings()) == settings()
    with pytest.raises(AIDisabled):
        registry.route("ping", settings())


def test_the_stored_key_module_reads_nothing_in_server_mode(monkeypatch):
    monkeypatch.setattr(stored_key, "_read", lambda: pytest.fail("the database must not be read in server mode"))
    plain = settings(app_mode="server")
    assert stored_key.apply(plain) is plain
