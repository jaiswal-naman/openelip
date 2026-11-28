"""Consent records (TRU-05, ARCHITECTURE 8.1): grant, withdraw and require over the append-only consents table.

The provider list is the one /v1/privacy/subprocessors shows, so these tests swap it the way that test does: a small
models.toml of their own and settings that pick one provider. The wording version moves with that list.
"""

import os
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.platform import auth, config, sessions
from app.platform.llm import registry
from app.platform.sessions import Principal, current_user
from app.trust import api, consent

TOML = """
[providers.alpha]
label = "Alpha AI"
adapter = "fake"
is_local = true
retention_note = "Invented provider for tests."
[providers.beta]
label = "Beta AI"
adapter = "fake"
is_local = true
retention_note = "Invented provider for tests."
[tasks.ping]
description = "Self-test."
data_class = "synthetic"
max_tokens = 8
timeout_seconds = 1
[tasks.ping.models.alpha]
model = "recorded"
[tasks.ping.models.beta]
model = "recorded"
"""


@pytest.fixture
def providers(monkeypatch, tmp_path):
    """providers("alpha"): act as an installation whose one active provider is that one."""
    (tmp_path / "models.toml").write_text(TOML, encoding="utf-8")
    monkeypatch.setattr(registry, "PATH", tmp_path / "models.toml")

    def use(name: str) -> None:
        # os.environ keeps the test database settings; only the provider changes
        settings = config.load({**os.environ, "LLM_PROVIDER": name})
        monkeypatch.setattr(config, "get", lambda: settings)

    use("alpha")
    return use


SECOND_WITHDRAWAL = """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version)
                       VALUES (:u, 'ai_processing', 'withdrawn', :g, 'x')"""


def make_user(conn, email: str = "ada@example.test"):
    return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


def act_as(conn, role: str, user_id=None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


@contextmanager
def refused(conn):
    """One statement that must fail, inside a savepoint so the test's transaction survives it."""
    with pytest.raises(DBAPIError):
        with conn.begin_nested():
            yield


def history(conn, user_id):
    sql = """SELECT purpose, action, withdraws_id, text_version FROM consents
             WHERE user_id = :u ORDER BY created_at, id"""
    return conn.execute(text(sql), {"u": user_id}).all()


def audit_rows(conn, user_id):
    sql = """SELECT event_type, actor_type, actor_user_id, entity_table, entity_id, details
             FROM audit_log WHERE subject_user_id = :u ORDER BY id"""
    return conn.execute(text(sql), {"u": user_id}).all()


def agree_to_ai(conn, user_id):
    return api.grant_consent(conn, user_id, "ai_processing", api.ai_processing_text_version())


def test_a_grant_satisfies_require(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    api.require_consent(conn, user, "ai_processing")  # does not raise


def test_without_a_grant_require_says_missing(conn, providers):
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, make_user(conn), "ai_processing")
    assert (refused_call.value.reason, refused_call.value.purpose) == ("missing", "ai_processing")


def test_a_grant_for_another_purpose_does_not_count(conn, providers):
    user = make_user(conn)
    api.grant_consent(conn, user, "terms", "terms-v1")
    with pytest.raises(api.ConsentRequired):
        api.require_consent(conn, user, "ai_processing")


def test_a_changed_provider_list_makes_the_old_grant_stale_until_the_person_agrees_again(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    providers("beta")
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "ai_processing")
    assert refused_call.value.reason == "providers_changed"
    agree_to_ai(conn, user)  # the new wording, as the person now sees it
    api.require_consent(conn, user, "ai_processing")


def test_the_text_version_is_twelve_characters_and_follows_the_provider_list(providers):
    first = api.ai_processing_text_version()
    assert first == api.ai_processing_text_version() and len(first) == 12
    providers("beta")
    assert api.ai_processing_text_version() != first


def test_the_text_version_follows_the_disclosure_version(providers, monkeypatch):
    first = api.ai_processing_text_version()
    monkeypatch.setattr(consent, "DISCLOSURE_VERSION", consent.DISCLOSURE_VERSION + "x")
    assert api.ai_processing_text_version() != first


def test_the_providers_are_named_as_the_disclosure_page_names_them(providers):
    assert consent.provider_names() == ["Alpha AI"]
    providers("beta")
    assert consent.provider_names() == ["Beta AI"]


IDENTIFIED_TASK = """
[tasks.chat]
description = "Chat."
data_class = "identified"
max_tokens = 8
timeout_seconds = 1
[tasks.chat.models.alpha]
model = "recorded"
"""


def use_shipped(monkeypatch, **env):
    """Act as an installation on the shipped config/models.toml, with these settings and no key but the ones named."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    settings = config.load({**os.environ, **blank, **env})
    monkeypatch.setattr(config, "get", lambda: settings)


def test_the_ai_consent_names_openai_with_its_retention_note_when_openai_is_the_provider(conn, monkeypatch):
    use_shipped(monkeypatch, LLM_PROVIDER="openai", OPENAI_API_KEY="k")
    (terms,) = consent.provider_terms()
    assert terms.name == "OpenAI" and "identified" in terms.data_classes  # what it receives, with the resume and chat
    assert (
        "up to 30 days" in terms.retention_note
        and "zero data retention needs OpenAI's approval" in terms.retention_note
    )
    ai = next(c for c in consent.current_consents(conn, make_user(conn)) if c.purpose == "ai_processing")
    assert ai.providers == ["OpenAI"] and ai.provider_terms == [terms]
    assert ai.current_text_version == api.ai_processing_text_version()


def test_someone_who_agreed_to_the_anthropic_wording_is_asked_again_when_the_provider_is_openai(conn, monkeypatch):
    use_shipped(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
    user = make_user(conn)
    agree_to_ai(conn, user)
    api.require_consent(conn, user, "ai_processing")
    anthropic_version = api.ai_processing_text_version()
    use_shipped(monkeypatch, LLM_PROVIDER="openai", OPENAI_API_KEY="k")
    assert api.ai_processing_text_version() != anthropic_version
    with pytest.raises(api.ConsentRequired) as asked:
        api.require_consent(conn, user, "ai_processing")
    assert asked.value.reason == "providers_changed"


def test_the_text_version_moves_when_a_provider_starts_receiving_identified_data(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    first = api.ai_processing_text_version()
    registry.PATH.write_text(TOML + IDENTIFIED_TASK, encoding="utf-8")  # the same provider, a new class of data
    assert consent.provider_names() == ["Alpha AI"] and api.ai_processing_text_version() != first
    with pytest.raises(api.ConsentRequired) as asked:
        api.require_consent(conn, user, "ai_processing")
    assert asked.value.reason == "providers_changed"


def test_the_text_version_moves_when_a_providers_retention_note_changes(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    first = api.ai_processing_text_version()
    registry.PATH.write_text(TOML.replace("Invented provider for tests.", "Kept 30 days.", 1), encoding="utf-8")
    assert api.ai_processing_text_version() != first
    with pytest.raises(api.ConsentRequired):
        api.require_consent(conn, user, "ai_processing")


def test_withdrawing_ends_the_grant_and_keeps_the_history(conn, providers):
    user = make_user(conn)
    grant = agree_to_ai(conn, user)
    assert api.withdraw_consent(conn, user, "ai_processing") == 1
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "ai_processing")
    assert refused_call.value.reason == "missing"
    assert [(r.action, r.withdraws_id) for r in history(conn, user)] == [("granted", None), ("withdrawn", grant)]


def test_withdrawing_twice_does_nothing_the_second_time(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    assert api.withdraw_consent(conn, user, "ai_processing") == 1
    assert api.withdraw_consent(conn, user, "ai_processing") == 0
    assert len(history(conn, user)) == 2


def test_withdrawing_with_nothing_granted_does_nothing(conn):
    user = make_user(conn)
    assert api.withdraw_consent(conn, user, "brief_email") == 0
    assert history(conn, user) == []


def test_withdrawing_ends_every_active_grant_of_that_purpose(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    providers("beta")
    agree_to_ai(conn, user)  # two grants are active: the old wording was never withdrawn
    assert api.withdraw_consent(conn, user, "ai_processing") == 2
    with pytest.raises(api.ConsentRequired):
        api.require_consent(conn, user, "ai_processing")


def test_the_database_allows_one_withdrawal_per_grant(conn, providers):
    user = make_user(conn)
    grant = agree_to_ai(conn, user)
    api.withdraw_consent(conn, user, "ai_processing")
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text(SECOND_WITHDRAWAL), {"u": user, "g": grant})


def test_a_withdrawal_cannot_end_another_persons_grant(conn, providers):
    a, b = make_user(conn, "a@example.test"), make_user(conn, "b@example.test")
    grant = agree_to_ai(conn, a)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text(SECOND_WITHDRAWAL), {"u": b, "g": grant})


def test_the_consent_history_cannot_be_rewritten(conn, providers):
    user = make_user(conn)
    agree_to_ai(conn, user)
    with refused(conn):
        conn.execute(text("UPDATE consents SET text_version = 'other'"))
    with refused(conn):
        conn.execute(text("DELETE FROM consents"))
    assert len(history(conn, user)) == 1


@pytest.mark.parametrize("purpose", ["introduction", "agent_access"])
def test_purposes_recorded_by_another_action_are_refused_here(conn, purpose):
    user = make_user(conn)
    with pytest.raises(ValueError):
        api.grant_consent(conn, user, purpose, "v1")
    with pytest.raises(ValueError):
        api.withdraw_consent(conn, user, purpose)
    with pytest.raises(ValueError):
        api.require_consent(conn, user, purpose)
    assert history(conn, user) == []


def test_an_unknown_purpose_is_refused(conn):
    with pytest.raises(ValueError):
        api.grant_consent(conn, make_user(conn), "everything", "v1")


def test_agreeing_again_to_the_same_wording_records_nothing_new(conn, providers):
    user = make_user(conn)
    first = agree_to_ai(conn, user)
    assert agree_to_ai(conn, user) == first
    assert len(history(conn, user)) == 1
    assert len(audit_rows(conn, user)) == 1


def test_other_purposes_keep_the_wording_the_person_saw_without_checking_it(conn):
    user = make_user(conn)
    api.grant_consent(conn, user, "brief_email", "brief-2026-11")
    api.require_consent(conn, user, "brief_email")
    assert history(conn, user)[0].text_version == "brief-2026-11"


def test_a_grant_and_a_withdrawal_are_audited_with_ids_and_codes_only(conn, providers):
    user = make_user(conn)
    grant = agree_to_ai(conn, user)
    api.withdraw_consent(conn, user, "ai_processing")
    granted, withdrawn = audit_rows(conn, user)
    assert (granted.event_type, granted.actor_type, granted.actor_user_id) == ("consent.granted", "candidate", user)
    assert (granted.entity_table, granted.entity_id) == ("consents", str(grant))
    assert granted.details == {"purpose": "ai_processing", "text_version": api.ai_processing_text_version()}
    assert (withdrawn.event_type, withdrawn.actor_type) == ("consent.withdrawn", "candidate")
    assert withdrawn.details == {"purpose": "ai_processing"}


def test_require_fails_closed_when_the_read_fails_and_the_transaction_stays_usable(conn, monkeypatch):
    def broken(conn, user_id, purpose):
        conn.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(consent, "_active_grants", broken)
    user = make_user(conn)
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "terms")
    assert refused_call.value.reason == "missing"
    assert conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": user}).scalar_one() == 1


def test_require_fails_closed_when_the_provider_list_cannot_be_read(conn, providers, monkeypatch):
    user = make_user(conn)
    agree_to_ai(conn, user)

    def unreadable():
        raise OSError("models.toml is gone")

    monkeypatch.setattr(consent, "provider_terms", unreadable)
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "ai_processing")
    assert refused_call.value.reason == "missing"


def test_a_person_sees_only_their_own_consents_and_cannot_record_one_for_someone_else(conn, providers):
    a, b = make_user(conn, "a@example.test"), make_user(conn, "b@example.test")
    agree_to_ai(conn, a)
    agree_to_ai(conn, b)
    act_as(conn, "app_api", a)
    assert conn.execute(text("SELECT count(*) FROM consents")).scalar_one() == 1
    api.grant_consent(conn, a, "terms", "terms-v1")  # their own: allowed, and the audit row too
    with refused(conn):
        api.grant_consent(conn, b, "terms", "terms-v1")
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_worker")
    assert conn.execute(text("SELECT count(*) FROM consents")).scalar_one() == 3


# ---- through the API: GET and PATCH /v1/me -------------------------------------------------------------------------
# The API opens its own connections and commits, so these use api_client (it empties the tables around each test) and
# read what was stored through the bootstrap engine.

PURPOSES = ["terms", "ai_processing", "brief_email", "open_to_introductions"]


@pytest.fixture
def person(api_client, engine, providers):
    """The API client acting as a signed-in person made here. The sign-in flow has its own tests (test_signin_flow)."""
    with engine.begin() as c:
        user = c.execute(text("INSERT INTO users (email) VALUES ('ada@example.test') RETURNING id")).scalar_one()
    api_client.app.dependency_overrides[current_user] = lambda: Principal(user)
    api_client.user = user
    return api_client


def patch(client, consents, key=None, **extra):
    return client.patch("/v1/me", json={"consents": consents, **extra}, headers={"Idempotency-Key": key or uuid4().hex})


def agree(purpose, version):
    return {"purpose": purpose, "granted": True, "text_version": version}


def stored(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def stored_consents(engine, user):
    sql = "SELECT purpose, action, text_version FROM consents WHERE user_id = :u ORDER BY created_at, id"
    return [tuple(row) for row in stored(engine, sql, u=user)]


def test_me_lists_every_consent_with_the_ai_wording_to_show_before_anything_is_granted(person):
    data = person.get("/v1/me").json()["data"]
    assert [c["purpose"] for c in data["consents"]] == PURPOSES
    assert not any(c["granted"] for c in data["consents"])
    ai = data["consents"][1]
    assert (ai["providers"], ai["current_text_version"]) == (["Alpha AI"], api.ai_processing_text_version())
    assert (ai["text_version"], ai["granted_at"]) == (None, None)
    assert ai["provider_terms"] == [
        {"name": "Alpha AI", "data_classes": ["synthetic"], "retention_note": "Invented provider for tests."}
    ]
    assert all(
        c["providers"] is None and c["provider_terms"] is None and c["current_text_version"] is None
        for c in data["consents"]
        if c is not ai
    )


def test_granting_the_ai_wording_in_force_records_it_and_me_shows_it(person, engine):
    version = api.ai_processing_text_version()
    response = patch(person, [agree("ai_processing", version)])
    assert response.status_code == 200 and response.json()["success"] is True
    ai = response.json()["data"]["consents"][1]
    assert (ai["granted"], ai["text_version"], ai["current_text_version"]) == (True, version, version)
    assert ai["granted_at"] is not None
    assert person.get("/v1/me").json()["data"]["consents"][1] == ai
    assert stored_consents(engine, person.user) == [("ai_processing", "granted", version)]
    (audit,) = stored(
        engine, "SELECT event_type, actor_user_id FROM audit_log WHERE subject_user_id = :u", u=person.user
    )
    assert tuple(audit) == ("consent.granted", person.user)
    with engine.connect() as c:
        api.require_consent(c, person.user, "ai_processing")  # the gate the AI features use now opens


def test_withdrawing_turns_it_off_at_once_and_keeps_the_history(person, engine):
    version = api.ai_processing_text_version()
    patch(person, [agree("ai_processing", version)])
    response = patch(person, [{"purpose": "ai_processing", "granted": False}])
    ai = response.json()["data"]["consents"][1]
    assert (response.status_code, ai["granted"], ai["text_version"], ai["granted_at"]) == (200, False, None, None)
    assert [row[1] for row in stored_consents(engine, person.user)] == ["granted", "withdrawn"]
    with engine.connect() as c, pytest.raises(api.ConsentRequired):
        api.require_consent(c, person.user, "ai_processing")


def test_a_stale_ai_wording_is_refused_with_the_current_version_and_nothing_is_recorded(person, engine):
    current = api.ai_processing_text_version()
    response = patch(person, [agree("terms", "terms-v1"), agree("ai_processing", "not-what-was-shown")])
    error = response.json()["error"]
    assert response.status_code == 409 and error["code"] == "conflict"
    assert error["details"] == {"state": "consent_text_changed", "current_version": current}
    assert stored_consents(engine, person.user) == []  # the terms grant before it was rolled back as well


def test_the_wording_a_person_saw_goes_stale_when_the_providers_change_between_view_and_agree(person, providers):
    shown = api.ai_processing_text_version()
    providers("beta")  # the operator changes the provider after the page was rendered
    response = patch(person, [agree("ai_processing", shown)])
    assert response.status_code == 409
    assert response.json()["error"]["details"]["current_version"] == api.ai_processing_text_version() != shown


def test_the_other_consents_keep_the_wording_the_person_saw(person, engine):
    response = patch(person, [agree("brief_email", "brief-2026-11"), agree("terms", "terms-2026-11")])
    assert response.status_code == 200
    assert sorted(stored_consents(engine, person.user)) == [
        ("brief_email", "granted", "brief-2026-11"),
        ("terms", "granted", "terms-2026-11"),
    ]


@pytest.mark.parametrize("purpose", ["introduction", "agent_access", "everything"])
def test_purposes_recorded_by_another_action_are_not_set_here(person, engine, purpose):
    response = patch(person, [agree(purpose, "v1")])
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"
    assert stored_consents(engine, person.user) == []


@pytest.mark.parametrize(
    "change",
    [
        {"purpose": "terms", "granted": True},  # no wording named
        {"purpose": "terms", "granted": True, "text_version": ""},
        {"purpose": "terms", "granted": True, "text_version": "has spaces"},
        {"purpose": "terms", "granted": True, "text_version": "x" * 65},
    ],
)
def test_a_grant_must_name_a_plausible_wording_version(person, engine, change):
    response = patch(person, [change])
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"
    assert stored_consents(engine, person.user) == []


def test_an_empty_or_missing_list_of_changes_is_refused(person):
    assert patch(person, []).status_code == 422
    empty = person.patch("/v1/me", json={}, headers={"Idempotency-Key": uuid4().hex})
    assert empty.status_code == 422


def test_the_person_is_the_session_never_anything_in_the_body(person, engine):
    with engine.begin() as c:
        other = c.execute(text("INSERT INTO users (email) VALUES ('mallory@example.test') RETURNING id")).scalar_one()
    change = {**agree("terms", "terms-v1"), "user_id": str(other)}
    response = patch(person, [change], user_id=str(other))
    assert response.status_code == 200
    assert stored_consents(engine, person.user) == [("terms", "granted", "terms-v1")]
    assert stored_consents(engine, other) == []


def test_the_same_idempotency_key_records_one_grant(person, engine):
    version = api.ai_processing_text_version()
    first = patch(person, [agree("ai_processing", version)], key="consent-key-0001")
    again = patch(person, [agree("ai_processing", version)], key="consent-key-0001")
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["meta"].get("idempotent_replay") is True
    assert len(stored_consents(engine, person.user)) == 1


def test_a_change_needs_a_session_a_key_and_json(api_client):
    body = {"consents": [agree("terms", "terms-v1")]}
    assert api_client.patch("/v1/me", json=body, headers={"Idempotency-Key": uuid4().hex}).status_code == 401
    api_client.app.dependency_overrides[current_user] = lambda: Principal(uuid4())
    assert api_client.patch("/v1/me", json=body).json()["error"]["code"] == "idempotency_key_missing"
    form = api_client.patch("/v1/me", data={"a": "b"}, headers={"Idempotency-Key": uuid4().hex})
    assert form.status_code == 415


def test_search_the_ledger_and_the_record_work_with_no_consent_at_all(person):
    assert person.get("/v1/jobs").status_code == 200
    assert person.get("/v1/control/costs", params={"group_by": "month"}).status_code == 200
    assert person.get("/v1/me").status_code == 200


@pytest.mark.parametrize("reason", ["missing", "providers_changed"])
def test_consent_required_answers_403_with_the_purpose_and_the_reason(api_client, reason):
    @api_client.app.get("/_probe")
    def probe():
        raise api.ConsentRequired(reason)

    response = api_client.get("/_probe")
    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (403, "consent_required")
    assert error["details"] == {"purpose": "ai_processing", "reason": reason}
    assert "AI" in error["message"]


def test_signing_in_returns_the_consents_too(api_client, engine, providers):
    auth._recent.clear()  # the per-address limit counts in process memory, across test modules
    email = "grace@example.test"
    assert api_client.post("/v1/auth/codes", json={"email": email}).status_code == 202
    sealed = stored(
        engine,
        """SELECT o.payload->>'code_sealed' FROM outbox o
           JOIN login_codes l ON l.id = CAST(o.payload->>'login_code_id' AS uuid)
           WHERE lower(l.email) = :e ORDER BY o.created_at DESC LIMIT 1""",
        e=email,
    )[0][0]
    signed_in = api_client.post("/v1/auth/sessions", json={"email": email, "code": sessions.open_sealed(sealed)})
    assert signed_in.status_code == 201
    assert [c["purpose"] for c in signed_in.json()["data"]["consents"]] == PURPOSES
    assert api_client.get("/v1/me").json()["data"]["consents"] == signed_in.json()["data"]["consents"]


# ---- local mode: the owner configured the provider, so the AI wording is recorded and kept current for them ----------


@pytest.fixture
def local_mode(providers, monkeypatch):
    """local_mode("beta"): the same swap of the provider as `providers`, in a process running as APP_MODE=local."""

    def use(name: str) -> None:
        settings = replace(config.load({**os.environ, "LLM_PROVIDER": name}), app_mode="local")
        monkeypatch.setattr(config, "get", lambda: settings)

    use("alpha")
    return use


def ai_state(conn, user):
    """What /v1/me shows for the AI wording: (granted, wording it was given under, wording in force)."""
    ai = next(c for c in api.current_consents(conn, user) if c.purpose == "ai_processing")
    return ai.granted, ai.text_version, ai.current_text_version


def test_local_owner_needs_no_click_the_gate_opens_and_the_grant_is_a_real_audited_row(conn, local_mode):
    user = make_user(conn)
    api.require_consent(conn, user, "ai_processing")  # does not raise
    version = api.ai_processing_text_version()
    assert history(conn, user) == [("ai_processing", "granted", None, version)]
    (audit,) = audit_rows(conn, user)
    assert (audit.event_type, audit.actor_type, audit.actor_user_id) == ("consent.granted", "candidate", user)
    assert audit.details == {"purpose": "ai_processing", "text_version": version, "source": consent.LOCAL_SETUP}


def test_local_owner_me_shows_the_wording_in_force_as_agreed_with_no_gate_call_first(conn, local_mode):
    user = make_user(conn)
    version = api.ai_processing_text_version()
    assert ai_state(conn, user) == (True, version, version)
    assert ai_state(conn, user) == (True, version, version)
    assert len(history(conn, user)) == 1  # reading again records nothing new


def test_local_consent_follows_the_provider_so_providers_changed_never_shows(conn, local_mode):
    user = make_user(conn)
    first = api.ai_processing_text_version()
    api.require_consent(conn, user, "ai_processing")
    local_mode("beta")
    second = api.ai_processing_text_version()
    assert second != first
    assert ai_state(conn, user) == (True, second, second)
    api.require_consent(conn, user, "ai_processing")  # no providers_changed
    assert [row.text_version for row in history(conn, user)] == [first, second]
    assert [e.details.get("source") for e in audit_rows(conn, user)] == [consent.LOCAL_SETUP] * 2


def test_an_explicit_grant_in_local_mode_is_recorded_as_the_persons_own_and_is_not_repeated(conn, local_mode):
    user = make_user(conn)
    agree_to_ai(conn, user)
    api.require_consent(conn, user, "ai_processing")
    (audit,) = audit_rows(conn, user)
    assert "source" not in audit.details and len(history(conn, user)) == 1


def test_a_local_withdrawal_stays_withdrawn_across_a_provider_change_until_the_owner_grants_again(conn, local_mode):
    user = make_user(conn)
    api.require_consent(conn, user, "ai_processing")
    assert api.withdraw_consent(conn, user, "ai_processing") == 1
    for provider in ("alpha", "beta", "alpha"):
        local_mode(provider)
        with pytest.raises(api.ConsentRequired) as refused_call:
            api.require_consent(conn, user, "ai_processing")
        assert refused_call.value.reason == "missing"
        granted, version, _ = ai_state(conn, user)
        assert (granted, version) == (False, None)
    assert [row.action for row in history(conn, user)] == ["granted", "withdrawn"]  # nothing was re-granted

    agree_to_ai(conn, user)  # only an explicit grant turns it back on...
    api.require_consent(conn, user, "ai_processing")
    local_mode("beta")  # ...and then it is kept current again
    api.require_consent(conn, user, "ai_processing")
    assert ai_state(conn, user)[:2] == (True, api.ai_processing_text_version())


def test_a_person_who_withdrew_a_grant_under_older_wording_stays_withdrawn_too(conn, local_mode):
    user = make_user(conn)
    api.grant_consent(conn, user, "ai_processing", "an-older-wording")  # a grant on file under other wording...
    api.withdraw_consent(conn, user, "ai_processing")  # ...withdrawn
    with pytest.raises(api.ConsentRequired):
        api.require_consent(conn, user, "ai_processing")
    assert [row.action for row in history(conn, user)] == ["granted", "withdrawn"]


def test_local_mode_leaves_other_purposes_and_synthetic_accounts_alone(conn, local_mode):
    user = make_user(conn)
    demo = conn.execute(
        text("INSERT INTO users (email, is_synthetic) VALUES ('demo@example.test', true) RETURNING id")
    ).scalar_one()
    api.require_consent(conn, user, "ai_processing")
    with pytest.raises(api.ConsentRequired):
        api.require_consent(conn, demo, "ai_processing")  # not the owner: nobody agreed for them
    assert ai_state(conn, demo)[0] is False
    assert history(conn, demo) == []
    assert [(c.purpose, c.granted) for c in api.current_consents(conn, user) if c.purpose != "ai_processing"] == [
        ("terms", False),
        ("brief_email", False),
        ("open_to_introductions", False),
    ]


def test_server_mode_still_needs_the_click_and_reading_records_nothing(conn, providers):
    user = make_user(conn)
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "ai_processing")
    assert refused_call.value.reason == "missing"
    assert ai_state(conn, user)[0] is False
    assert history(conn, user) == []
    agree_to_ai(conn, user)
    providers("beta")
    with pytest.raises(api.ConsentRequired) as stale:
        api.require_consent(conn, user, "ai_processing")
    assert stale.value.reason == "providers_changed"
    assert len(history(conn, user)) == 1


def test_local_gate_fails_closed_when_the_grant_cannot_be_recorded_and_the_transaction_stays_usable(
    conn, local_mode, monkeypatch
):
    def broken(*args, **kwargs):
        conn.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(consent, "grant_consent", broken)
    user = make_user(conn)
    with pytest.raises(api.ConsentRequired) as refused_call:
        api.require_consent(conn, user, "ai_processing")
    assert refused_call.value.reason == "missing"
    assert conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": user}).scalar_one() == 1


def test_over_the_api_a_local_owner_sees_consent_given_and_a_withdrawal_sticks(person, local_mode, engine):
    version = api.ai_processing_text_version()
    ai = person.get("/v1/me").json()["data"]["consents"][1]
    assert (ai["granted"], ai["text_version"], ai["current_text_version"]) == (True, version, version)
    withdrawn = patch(person, [{"purpose": "ai_processing", "granted": False}]).json()["data"]["consents"][1]
    assert (withdrawn["granted"], withdrawn["text_version"]) == (False, None)
    local_mode("beta")
    again = person.get("/v1/me").json()["data"]["consents"][1]
    assert (again["granted"], again["current_text_version"]) == (False, api.ai_processing_text_version())
    assert [row[1] for row in stored_consents(engine, person.user)] == ["granted", "withdrawn"]
    granted = patch(person, [agree("ai_processing", api.ai_processing_text_version())]).json()["data"]["consents"][1]
    assert granted["granted"] is True
