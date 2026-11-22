"""Consent records (TRU-05, ARCHITECTURE 8.1): grant, withdraw and require over the append-only consents table.

The provider list is the one /v1/privacy/subprocessors shows, so these tests swap it the way that test does: a small
models.toml of their own and settings that pick one provider. The wording version moves with that list.
"""

import os
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.platform import config
from app.platform.llm import registry
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

    monkeypatch.setattr(consent, "provider_names", unreadable)
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
