"""Agent tokens over HTTP (plan 09-04 task 1; NET-01, NET-03): issue, list, revoke and refusals for a candidate and for
an organization, the secret shown once and stored only as its hash, and the agent context of a transaction.

The API opens its own connections as app_api, so row-level security is in force; a person signs in by overriding the
session dependency. Everything here is invented.
"""

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.agentnet import tokens
from app.platform import config, db
from app.platform.sessions import Principal, current_user
from tests.agentnet.factories import committed_world
from tests.hiring.factories import make_member

CONSENT = tokens.AGENT_ACCESS_TEXT_VERSION


@pytest.fixture
def desk(engine, api_client):
    d = SimpleNamespace(client=api_client, engine=engine, world=committed_world(engine))
    d.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    return d


def key():
    return {"Idempotency-Key": str(uuid4())}


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def issue_mine(desk, **over):
    body = {"client_name": "Demo agent", "scopes": ["matches:read"], "consent_text_version": CONSENT, **over}
    return desk.client.post("/v1/me/agent-tokens", json=body, headers=key())


def issue_org(desk, **over):
    body = {"client_name": "Halden agent", "scopes": ["twin:ask", "introductions:request"], **over}
    return desk.client.post(f"/v1/orgs/{desk.world.org.id}/agent-tokens", json=body, headers=key())


def test_a_candidate_issues_a_token_and_only_its_hash_is_stored(desk):
    desk.sign_in(desk.world.candidate.id)

    response = issue_mine(desk, scopes=["matches:read", "explanations:read"], expires_in_days=14)

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["token"].startswith("agt_") and len(data["token"]) == 36
    assert (data["is_shown_once"], data["principal"], data["status"], data["mcp_path"]) == (
        True,
        "candidate",
        "active",
        "/mcp",
    )
    assert response.headers["cache-control"] == "no-store"
    stored = scalar(desk.engine, "SELECT token_sha256 FROM agent_tokens WHERE id = :i", i=data["id"])
    assert bytes(stored) == hashlib.sha256(data["token"].encode()).digest()
    # the secret is nowhere else: not in a column of the token row, not in the idempotency record
    assert scalar(desk.engine, "SELECT count(*) FROM agent_tokens t WHERE t::text LIKE :s", s=f"%{data['token']}%") == 0
    assert (
        scalar(desk.engine, "SELECT count(*) FROM idempotency_keys k WHERE k::text LIKE :s", s=f"%{data['token']}%")
        == 0
    )
    assert (
        scalar(desk.engine, "SELECT count(*) FROM consents WHERE purpose = 'agent_access' AND action = 'granted'") == 1
    )
    assert scalar(desk.engine, "SELECT count(*) FROM audit_log WHERE event_type = 'agentnet.token_issued'") == 1


def test_a_repeated_idempotency_key_returns_the_same_token_without_a_second_secret(desk):
    desk.sign_in(desk.world.candidate.id)
    headers = key()
    body = {"client_name": "Demo agent", "scopes": ["matches:read"], "consent_text_version": CONSENT}

    first = desk.client.post("/v1/me/agent-tokens", json=body, headers=headers).json()["data"]
    again = desk.client.post("/v1/me/agent-tokens", json=body, headers=headers)

    assert again.status_code == 201
    repeat = again.json()["data"]
    assert repeat["id"] == first["id"] and repeat["token"] is None and repeat["is_shown_once"] is False
    assert scalar(desk.engine, "SELECT count(*) FROM agent_tokens") == 1


def test_a_stale_consent_wording_is_403_consent_required_and_nothing_is_stored(desk):
    desk.sign_in(desk.world.candidate.id)

    response = issue_mine(desk, consent_text_version="agent-access-2000-01")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "consent_required"
    assert scalar(desk.engine, "SELECT count(*) FROM agent_tokens") == 0
    assert scalar(desk.engine, "SELECT count(*) FROM consents WHERE purpose = 'agent_access'") == 0


@pytest.mark.parametrize(
    "over",
    [
        {"scopes": ["twin:ask"]},  # an organization scope on a candidate token
        {"scopes": ["matches:read", "nonsense"]},
        {"scopes": []},
        {"expires_in_days": 0},
        {"expires_in_days": 91},
        {"client_name": ""},
    ],
)
def test_a_scope_or_lifetime_outside_the_rules_is_422(desk, over):
    desk.sign_in(desk.world.candidate.id)

    response = issue_mine(desk, **over)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
    assert scalar(desk.engine, "SELECT count(*) FROM agent_tokens") == 0


def test_an_owner_issues_an_organization_token_and_a_plain_member_cannot(desk):
    desk.sign_in(desk.world.org.owner)
    response = issue_org(desk)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["principal"], data["scopes"]) == ("organization", ["twin:ask", "introductions:request"])
    assert (
        scalar(desk.engine, "SELECT organization_id FROM agent_tokens WHERE id = :i", i=data["id"]) == desk.world.org.id
    )

    desk.sign_in(desk.world.member.user)
    assert issue_org(desk).status_code == 403

    desk.sign_in(desk.world.candidate.id)  # not a member at all: the organization is not theirs to know about
    assert issue_org(desk).status_code == 404
    assert issue_org(desk, scopes=["matches:read"]).status_code in (404, 422)


def test_an_organization_token_refuses_a_candidate_scope(desk):
    desk.sign_in(desk.world.org.owner)

    response = issue_org(desk, scopes=["matches:read"])

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


def test_the_list_shows_a_derived_status_and_never_the_secret_or_the_hash(desk):
    desk.sign_in(desk.world.candidate.id)
    live = issue_mine(desk).json()["data"]
    gone = issue_mine(desk, client_name="Old agent").json()["data"]
    expired = issue_mine(desk, client_name="Stale agent").json()["data"]
    assert desk.client.delete(f"/v1/me/agent-tokens/{gone['id']}", headers=key()).status_code == 204
    with desk.engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_tokens SET expires_at = now() - interval '1 day' WHERE id = :i"), {"i": expired["id"]}
        )

    response = desk.client.get("/v1/me/agent-tokens")

    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]["tokens"]}
    assert (rows[live["id"]]["status"], rows[gone["id"]]["status"], rows[expired["id"]]["status"]) == (
        "active",
        "revoked",
        "expired",
    )
    assert set(rows[live["id"]]) == {
        "id",
        "client_name",
        "principal",
        "scopes",
        "status",
        "expires_at",
        "last_used_at",
        "created_at",
        "refused_last_7_days",
    }
    assert live["token"] not in response.text and "sha256" not in response.text


def test_revoking_ends_the_token_and_withdraws_its_consent_and_is_audited(desk):
    desk.sign_in(desk.world.candidate.id)
    issued = issue_mine(desk).json()["data"]
    digest = hashlib.sha256(issued["token"].encode()).digest()
    resolve = "SELECT count(*) FROM resolve_agent_token(:h)"
    assert scalar(desk.engine, resolve, h=digest) == 1

    assert desk.client.delete(f"/v1/me/agent-tokens/{issued['id']}", headers=key()).status_code == 204

    assert scalar(desk.engine, resolve, h=digest) == 0
    assert (
        scalar(desk.engine, "SELECT count(*) FROM consents WHERE purpose = 'agent_access' AND action = 'withdrawn'")
        == 1
    )
    assert scalar(desk.engine, "SELECT count(*) FROM audit_log WHERE event_type = 'agentnet.token_revoked'") == 1
    # a second revoke is a no-op, not an error and not a second withdrawal
    assert desk.client.delete(f"/v1/me/agent-tokens/{issued['id']}", headers=key()).status_code == 204
    assert scalar(desk.engine, "SELECT count(*) FROM consents WHERE action = 'withdrawn'") == 1


def test_another_persons_or_organizations_token_is_404(desk):
    desk.sign_in(desk.world.candidate.id)
    mine = issue_mine(desk).json()["data"]
    desk.sign_in(desk.world.org.owner)
    org = issue_org(desk).json()["data"]
    with desk.engine.begin() as conn:
        stranger = make_member(conn, desk.world.org)  # a plain member of the same organization

    desk.sign_in(desk.world.org.owner)  # the owner cannot touch the candidate's token
    assert desk.client.delete(f"/v1/me/agent-tokens/{mine['id']}", headers=key()).status_code == 404
    assert desk.client.get(f"/v1/me/agent-tokens/{mine['id']}/refusals").status_code == 404
    desk.sign_in(desk.world.candidate.id)  # and the candidate cannot reach the organization's
    assert (
        desk.client.delete(f"/v1/orgs/{desk.world.org.id}/agent-tokens/{org['id']}", headers=key()).status_code == 404
    )
    assert desk.client.get(f"/v1/orgs/{desk.world.org.id}/agent-tokens").status_code == 404
    desk.sign_in(stranger.user)  # a plain member may not list or revoke
    assert desk.client.get(f"/v1/orgs/{desk.world.org.id}/agent-tokens").status_code == 403
    assert (
        desk.client.delete(f"/v1/orgs/{desk.world.org.id}/agent-tokens/{org['id']}", headers=key()).status_code == 403
    )
    assert scalar(desk.engine, "SELECT count(*) FROM agent_tokens WHERE revoked_at IS NOT NULL") == 0


def test_the_organization_lists_revokes_and_reads_refusals_of_its_tokens(desk):
    desk.sign_in(desk.world.org.owner)
    issued = issue_org(desk).json()["data"]
    token = issued["id"]
    with desk.engine.begin() as conn:  # two refused calls, written as the gate will write them (the worker may insert)
        for tool, reason in (("ask_twin", "insufficient_scope"), ("request_introduction", "role_not_open")):
            conn.execute(
                text(
                    """INSERT INTO audit_log (event_type, actor_type, actor_user_id, organization_id, agent_token_id,
                                              details)
                       VALUES ('agentnet.agent_call_refused', 'outside_agent', :u, :o, :t,
                               jsonb_build_object('tool', CAST(:tool AS text), 'reason', CAST(:reason AS text)))"""
                ),
                {"u": desk.world.org.owner, "o": desk.world.org.id, "t": token, "tool": tool, "reason": reason},
            )

    listing = desk.client.get(f"/v1/orgs/{desk.world.org.id}/agent-tokens").json()["data"]["tokens"]
    assert [(t["id"], t["refused_last_7_days"]) for t in listing] == [(token, 2)]
    refusals = desk.client.get(f"/v1/orgs/{desk.world.org.id}/agent-tokens/{token}/refusals").json()["data"]["refusals"]
    assert [(r["tool"], r["reason"]) for r in refusals] == [
        ("request_introduction", "role_not_open"),
        ("ask_twin", "insufficient_scope"),
    ]
    assert all(set(r) == {"created_at", "tool", "reason"} for r in refusals)

    assert desk.client.delete(f"/v1/orgs/{desk.world.org.id}/agent-tokens/{token}", headers=key()).status_code == 204
    status = desk.client.get(f"/v1/orgs/{desk.world.org.id}/agent-tokens").json()["data"]["tokens"][0]["status"]
    assert status == "revoked"


def test_agent_tokens_do_not_work_on_v1(desk):
    desk.sign_in(desk.world.candidate.id)
    secret = issue_mine(desk).json()["data"]["token"]
    desk.client.app.dependency_overrides.clear()  # no session at all: only the bearer token
    desk.client.cookies.clear()

    assert desk.client.get("/v1/me", headers={"Authorization": f"Bearer {secret}"}).status_code == 401
    assert desk.client.get("/v1/me/agent-tokens", headers={"Authorization": f"Bearer {secret}"}).status_code == 401


# ---- the agent context of a transaction ------------------------------------------------------------------------------


def test_a_transaction_carries_the_agent_context_for_itself_only(engine):
    token = uuid4()
    show = "SELECT current_setting('app.agent_token_id', true), current_setting('app.token_scopes', true)"
    with db.transaction(agent=db.AgentContext(token, ("matches:read", "explanations:read"))) as conn:
        assert tuple(conn.execute(text(show)).one()) == (str(token), "matches:read,explanations:read")
    with db.transaction() as conn:  # the pooled connection comes back clean, and without `agent` nothing is set
        assert tuple(conn.execute(text(show)).one()) in (("", ""), (None, None))


# ---- configuration -------------------------------------------------------------------------------------------------


def test_mcp_origins_default_to_loopback_and_parse_a_comma_list():
    assert config.load({}).mcp_allowed_origins == ("http://127.0.0.1:8080", "http://localhost:8080")
    parsed = config.load({"MCP_ALLOWED_ORIGINS": "http://localhost:3000, http://[::1]:8080"})
    assert parsed.mcp_allowed_origins == ("http://localhost:3000", "http://[::1]:8080")


def test_a_remote_origin_stops_start_up_unless_it_is_allowed_on_purpose():
    with pytest.raises(config.ConfigError, match="MCP_ALLOW_REMOTE_ORIGINS"):
        config.load({"MCP_ALLOWED_ORIGINS": "https://agents.example"})
    with pytest.raises(config.ConfigError):
        config.load({"MCP_ALLOWED_ORIGINS": "http://localhost.evil.test:8080"})
    allowed = config.load({"MCP_ALLOWED_ORIGINS": "https://agents.example", "MCP_ALLOW_REMOTE_ORIGINS": "true"})
    assert allowed.mcp_allowed_origins == ("https://agents.example",)
