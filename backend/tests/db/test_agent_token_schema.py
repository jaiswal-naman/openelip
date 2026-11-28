"""Revision p9_0901_twin_agentnet, the token side (plan 09-01; NET-01, NET-02, NET-03; DATA_MODEL 3.11, 4.x, 5.2).

A candidate's token is bound to that candidate's live agent_access consent and an organization's token to an owner or
admin; each kind carries only its own scopes. resolve_agent_token answers while the token is unexpired, unrevoked and
its consent (or its issuer's membership) is live, and never otherwise. touch_agent_token, role_ask_bucket and
twin_message_citations are SECURITY DEFINER functions that check who is asking. Every row is invented
(tests/twin/factories.py); each test runs in the `conn` fixture's transaction.
"""

import hashlib
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import make_action, make_run
from tests.hiring.factories import make_ask, make_candidate, make_member
from tests.twin.factories import (
    TwinWorld,
    candidate_token,
    employer_question,
    org_token,
    twin_session,
    twin_world,
    withdraw,
)


@contextmanager
def refused(conn: Connection):
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def act_as(
    conn: Connection, role: str, user: UUID | None = None, *, token: UUID | None = None, scopes: str = ""
) -> None:
    conn.execute(text("RESET ROLE"))
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if user else ""})
    conn.execute(text("SELECT set_config('app.agent_token_id', :t, true)"), {"t": str(token) if token else ""})
    conn.execute(text("SELECT set_config('app.token_scopes', :s, true)"), {"s": scopes})


def as_bootstrap(conn: Connection) -> None:
    conn.execute(text("RESET ROLE"))


def resolve(conn: Connection, secret: str):
    return conn.execute(
        text("SELECT token_id, user_id, organization_id, scopes::text[] FROM resolve_agent_token(:h)"),
        {"h": hashlib.sha256(secret.encode()).digest()},
    ).first()


@pytest.fixture
def world(conn) -> TwinWorld:
    return twin_world(conn)


# ---- issuing --------------------------------------------------------------------------------------------------


def test_an_organization_token_with_a_candidate_scope_and_a_candidate_token_with_twin_ask_are_refused(conn, world):
    with refused(conn) as caught:
        org_token(conn, world.org, world.org.owner, ["matches:read"])
    assert caught.value.orig.diag.constraint_name == "agent_tokens_principal_scopes"
    with refused(conn) as caught:
        candidate_token(conn, world.candidate.id, ["twin:ask"])
    assert caught.value.orig.diag.constraint_name == "agent_tokens_principal_scopes"
    with refused(conn):  # no scope at all
        org_token(conn, world.org, world.org.owner, [])


def test_each_kind_takes_its_own_scopes(conn, world):
    candidate_token(conn, world.candidate.id, ["matches:read", "explanations:read"])
    org_token(conn, world.org, world.org.owner, ["twin:ask", "introductions:request"])


def test_a_candidate_token_needs_a_live_agent_access_consent_of_that_candidate(conn, world):
    token = candidate_token(conn, world.candidate.id)
    consent = one(conn, "SELECT consent_id FROM agent_tokens WHERE id = :t", t=token.id)
    secret = "agt_" + "x" * 32
    insert = """INSERT INTO agent_tokens (principal, user_id, consent_id, client_name, token_sha256, scopes, expires_at)
                VALUES ('candidate', :u, :c, 'x', :h, ARRAY['matches:read']::token_scope[], now() + interval '1 day')"""
    digest = hashlib.sha256(secret.encode()).digest()
    with refused(conn) as caught:  # another person's consent
        conn.execute(text(insert), {"u": world.candidate.id, "c": uuid4(), "h": digest})
    assert "foreign key" in str(caught.value) or "consent_missing" in str(caught.value)
    other = make_candidate(conn)
    with refused(conn) as caught:  # the consent is the first candidate's, the token is the second's
        conn.execute(text(insert), {"u": other.id, "c": consent, "h": digest})
    assert "consent_missing" in str(caught.value)
    wrong_purpose = one(
        conn,
        "SELECT id FROM consents WHERE user_id = :u AND purpose = 'open_to_introductions'",
        u=world.candidate.id,
    )
    with refused(conn) as caught:  # a consent of another purpose
        conn.execute(text(insert), {"u": world.candidate.id, "c": wrong_purpose, "h": digest})
    assert "consent_missing" in str(caught.value)
    withdraw(conn, consent)
    with refused(conn) as caught:  # withdrawn
        conn.execute(text(insert), {"u": world.candidate.id, "c": consent, "h": digest})
    assert "consent_missing" in str(caught.value)


def test_an_organization_token_is_issued_by_an_owner_or_admin_not_a_plain_member(conn, world):
    org_token(conn, world.org, world.org.owner)
    admin = make_member(conn, world.org, role="admin")
    org_token(conn, world.org, admin.user)
    with refused(conn) as caught:
        org_token(conn, world.org, world.member.user)
    assert "owner or admin" in str(caught.value)
    with refused(conn):  # an admin of another organization
        org_token(conn, world.org, twin_world(conn).org.owner)


def test_only_the_hash_is_stored(conn, world):
    token = candidate_token(conn, world.candidate.id)
    stored = one(conn, "SELECT token_sha256 FROM agent_tokens WHERE id = :t", t=token.id)
    assert bytes(stored) == hashlib.sha256(token.secret.encode()).digest()
    assert token.secret.startswith("agt_") and len(token.secret) == 36
    columns = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'agent_tokens'")
    ).scalars()
    assert not any(
        token.secret in str(one(conn, f'SELECT "{c}"::text FROM agent_tokens WHERE id = :t', t=token.id))
        for c in columns
    )


# ---- resolve_agent_token ------------------------------------------------------------------------------------------


def test_a_live_candidate_token_resolves_to_its_owner_and_scopes(conn, world):
    token = candidate_token(conn, world.candidate.id, ["matches:read"])
    row = resolve(conn, token.secret)
    assert row == (token.id, world.candidate.id, None, ["matches:read"])
    assert resolve(conn, "agt_" + "z" * 32) is None  # an unknown secret


def test_a_live_organization_token_resolves_to_its_issuer_and_organization(conn, world):
    token = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    assert resolve(conn, token.secret) == (token.id, world.org.owner, world.org.id, ["twin:ask"])


def test_a_revoked_or_expired_token_resolves_to_nothing(conn, world):
    revoked = candidate_token(conn, world.candidate.id)
    expired = org_token(conn, world.org, world.org.owner)
    assert resolve(conn, revoked.secret) and resolve(conn, expired.secret)
    conn.execute(text("UPDATE agent_tokens SET revoked_at = now() WHERE id = :t"), {"t": revoked.id})
    conn.execute(
        text("UPDATE agent_tokens SET expires_at = now() - interval '1 second' WHERE id = :t"), {"t": expired.id}
    )
    assert resolve(conn, revoked.secret) is None
    assert resolve(conn, expired.secret) is None


def test_withdrawing_the_agent_access_consent_kills_the_candidate_token(conn, world):
    token = candidate_token(conn, world.candidate.id)
    assert resolve(conn, token.secret)
    withdraw(conn, one(conn, "SELECT consent_id FROM agent_tokens WHERE id = :t", t=token.id))
    assert resolve(conn, token.secret) is None
    assert one(conn, "SELECT revoked_at IS NULL FROM agent_tokens WHERE id = :t", t=token.id)  # no write needed


def test_an_organization_token_stops_when_its_issuer_is_no_longer_an_active_member(conn, world):
    admin = make_member(conn, world.org, role="admin")
    token = org_token(conn, world.org, admin.user)
    assert resolve(conn, token.secret)
    conn.execute(text("DELETE FROM org_members WHERE id = :m"), {"m": admin.id})
    assert resolve(conn, token.secret) is None


def test_resolve_agent_token_is_callable_by_the_api_login_alone(conn, world):
    token = candidate_token(conn, world.candidate.id)
    digest = hashlib.sha256(token.secret.encode()).digest()
    act_as(conn, "app_api")  # no user yet: this is the gate, before any request context exists
    assert one(conn, "SELECT count(*) FROM resolve_agent_token(:h)", h=digest) == 1
    for role in ("app_worker", "app_match"):
        act_as(conn, role)
        with refused(conn) as caught:
            conn.execute(text("SELECT * FROM resolve_agent_token(:h)"), {"h": digest})
        assert caught.value.orig.sqlstate == "42501", role


# ---- touch_agent_token --------------------------------------------------------------------------------------------


def test_touch_sets_last_used_once_a_minute_and_only_for_the_tokens_own_request(conn, world):
    token = candidate_token(conn, world.candidate.id)
    assert one(conn, "SELECT last_used_at FROM agent_tokens WHERE id = :t", t=token.id) is None
    act_as(conn, "app_api", world.candidate.id, token=token.id, scopes="matches:read")
    conn.execute(text("SELECT touch_agent_token(:t)"), {"t": token.id})
    as_bootstrap(conn)
    first = one(conn, "SELECT last_used_at FROM agent_tokens WHERE id = :t", t=token.id)
    assert first is not None
    conn.execute(
        text("UPDATE agent_tokens SET last_used_at = now() - interval '30 seconds' WHERE id = :t"), {"t": token.id}
    )
    act_as(conn, "app_api", world.candidate.id, token=token.id, scopes="matches:read")
    conn.execute(text("SELECT touch_agent_token(:t)"), {"t": token.id})  # within a minute: no move
    as_bootstrap(conn)
    assert one(conn, "SELECT now() - last_used_at > interval '29 seconds' FROM agent_tokens WHERE id = :t", t=token.id)
    conn.execute(
        text("UPDATE agent_tokens SET last_used_at = now() - interval '2 minutes' WHERE id = :t"), {"t": token.id}
    )
    act_as(conn, "app_api", world.candidate.id, token=token.id, scopes="matches:read")
    conn.execute(text("SELECT touch_agent_token(:t)"), {"t": token.id})
    as_bootstrap(conn)
    assert one(conn, "SELECT now() - last_used_at < interval '1 minute' FROM agent_tokens WHERE id = :t", t=token.id)


def test_touch_refuses_another_persons_token_and_another_tokens_context(conn, world):
    mine = candidate_token(conn, world.candidate.id)
    other = make_candidate(conn)
    theirs = candidate_token(conn, other.id)
    act_as(conn, "app_api", other.id)  # a person's own request, not the issuer
    with refused(conn) as caught:
        conn.execute(text("SELECT touch_agent_token(:t)"), {"t": mine.id})
    assert caught.value.orig.sqlstate == "42501"
    act_as(conn, "app_api", other.id, token=theirs.id, scopes="matches:read")  # another token's context
    with refused(conn) as caught:
        conn.execute(text("SELECT touch_agent_token(:t)"), {"t": mine.id})
    assert caught.value.orig.sqlstate == "42501"
    act_as(conn, "app_api", world.candidate.id)  # the issuer, signed in as themselves, may touch their own
    conn.execute(text("SELECT touch_agent_token(:t)"), {"t": mine.id})
    for role in ("app_worker", "app_match"):
        act_as(conn, role, world.candidate.id)
        with refused(conn) as caught:
            conn.execute(text("SELECT touch_agent_token(:t)"), {"t": mine.id})
        assert caught.value.orig.sqlstate == "42501", role


# ---- role_ask_bucket ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "bucket"), [(0, "under_5"), (3, "under_5"), (5, "5_to_9"), (7, "5_to_9")])
def test_role_ask_bucket_counts_asked_introductions_in_buckets(conn, world, asked, bucket):
    # the world's own introduction is accepted and does not count
    for _ in range(asked):
        make_ask(conn, world.role, make_candidate(conn))
    act_as(conn, "app_api", world.member.user)
    assert one(conn, "SELECT role_ask_bucket(:r)", r=world.role.id) == bucket


def test_role_ask_bucket_reaches_the_larger_buckets(conn, world):
    for _ in range(10):
        make_ask(conn, world.role, make_candidate(conn))
    act_as(conn, "app_api", world.member.user)
    assert one(conn, "SELECT role_ask_bucket(:r)", r=world.role.id) == "10_to_24"
    as_bootstrap(conn)
    for _ in range(15):
        make_ask(conn, world.role, make_candidate(conn))
    act_as(conn, "app_api", world.member.user)
    assert one(conn, "SELECT role_ask_bucket(:r)", r=world.role.id) == "25_plus"


def test_role_ask_bucket_is_for_members_of_the_roles_organization_and_scoped_tokens(conn, world):
    stranger = twin_world(conn)
    act_as(conn, "app_api", stranger.member.user)
    with refused(conn) as caught:
        conn.execute(text("SELECT role_ask_bucket(:r)"), {"r": world.role.id})
    assert caught.value.orig.sqlstate == "42501"
    with refused(conn) as caught:  # a role that does not exist
        conn.execute(text("SELECT role_ask_bucket(:r)"), {"r": uuid4()})
    assert caught.value.orig.sqlstate == "42501"
    act_as(conn, "app_api", world.candidate.id)  # a candidate, not a member
    with refused(conn) as caught:
        conn.execute(text("SELECT role_ask_bucket(:r)"), {"r": world.role.id})
    assert caught.value.orig.sqlstate == "42501"
    as_bootstrap(conn)
    asker = org_token(conn, world.org, world.org.owner, ["introductions:request"])
    twin_only = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    act_as(conn, "app_api", world.org.owner, token=asker.id, scopes="introductions:request")
    assert one(conn, "SELECT role_ask_bucket(:r)", r=world.role.id) == "under_5"
    act_as(conn, "app_api", world.org.owner, token=twin_only.id, scopes="twin:ask")
    with refused(conn) as caught:
        conn.execute(text("SELECT role_ask_bucket(:r)"), {"r": world.role.id})
    assert caught.value.orig.sqlstate == "42501"


# ---- twin_message_citations ----------------------------------------------------------------------------------------


@pytest.fixture
def answered(conn, world):
    session = twin_session(conn, world)
    question = employer_question(conn, session)
    run = make_run(conn, world.candidate.id, None, "twin")
    action = make_action(
        conn, run, action_type="twin_answer", status="executed", payload={"kind": "answer"}, policy_decision="ask"
    )
    facts = world.candidate.record.fact_ids
    answer = one(
        conn,
        """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                      cited_fact_ids, in_reply_to, agent_action_id)
           VALUES (:s, :u, :o, 'twin', 'Invented answer.', 'skills', 'answered', CAST(:f AS uuid[]), :q, :a)
           RETURNING id""",
        s=session,
        u=world.candidate.id,
        o=world.org.id,
        f=[facts[1], facts[0]],
        q=question,
        a=action,
    )
    unknown = one(
        conn,
        """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                      in_reply_to)
           VALUES (:s, :u, :o, 'twin', 'Holding.', 'skills', 'unknown', :q) RETURNING id""",
        s=session,
        u=world.candidate.id,
        o=world.org.id,
        q=question,
    )
    statements = [one(conn, "SELECT statement FROM profile_facts WHERE id = :f", f=f) for f in (facts[1], facts[0])]
    return answer, unknown, statements


def citations(conn: Connection, message: UUID) -> list[str]:
    return list(conn.execute(text("SELECT quote FROM twin_message_citations(:m)"), {"m": message}).scalars())


def test_the_candidate_and_a_member_of_the_organization_read_the_cited_statements_in_order(conn, world, answered):
    answer, _, statements = answered
    act_as(conn, "app_api", world.candidate.id)
    assert citations(conn, answer) == statements
    act_as(conn, "app_api", world.member.user)
    assert citations(conn, answer) == statements
    as_bootstrap(conn)
    token = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    act_as(conn, "app_api", world.org.owner, token=token.id, scopes="twin:ask")
    assert citations(conn, answer) == statements


def test_nothing_for_another_organization_a_token_without_twin_ask_a_candidate_token_or_an_unknown_message(
    conn, world, answered
):
    answer, unknown, _ = answered
    act_as(conn, "app_api", twin_world(conn).member.user)
    assert citations(conn, answer) == []
    as_bootstrap(conn)
    asker = org_token(conn, world.org, world.org.owner, ["introductions:request"])
    act_as(conn, "app_api", world.org.owner, token=asker.id, scopes="introductions:request")
    assert citations(conn, answer) == []
    as_bootstrap(conn)
    mine = candidate_token(conn, world.candidate.id, ["explanations:read"])
    act_as(conn, "app_api", world.candidate.id, token=mine.id, scopes="explanations:read")
    assert citations(conn, answer) == []  # a candidate's agent does not read the employer's conversation
    act_as(conn, "app_api", world.member.user)
    assert citations(conn, unknown) == []  # only an answered message cites anything
    assert citations(conn, uuid4()) == []
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        conn.execute(text("SELECT * FROM twin_message_citations(:m)"), {"m": answer})
    assert caught.value.orig.sqlstate == "42501"
