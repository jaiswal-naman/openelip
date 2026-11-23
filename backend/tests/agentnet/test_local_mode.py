"""Local mode and the MCP server (plan 09-04; APP_MODE=local): the one owner of the installation is a candidate and an
employer at once, signs in with no code, and issues tokens in the app exactly as in server mode. The MCP server stays
local-only: it answers only to this computer's own host names, as the local-mode routes do, and a rebound domain gets
421.
Everything here is invented.
"""

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.platform import config
from tests.agentnet.factories import call, rpc
from tests.conftest import _empty_tables

HOME = "http://localhost:8080"


@pytest.fixture
def owner(engine, monkeypatch):
    """The app in local mode, with the owner signed in (no emailed code) from localhost:8080."""
    _empty_tables(engine)
    local = replace(
        config.get(),
        app_mode="local",
        llm_provider="anthropic",
        llm_provider_is_set=False,
        anthropic_api_key=None,
        openai_api_key=None,
        llm_base_url=None,
    )
    monkeypatch.setattr(config, "get", lambda: local)
    with TestClient(create_app(), base_url=HOME) as client:
        assert client.post("/v1/auth/local", json={}).status_code == 201
        yield client
    _empty_tables(engine)


def write(client, method, path, **kwargs):
    return client.request(method, path, headers={"Idempotency-Key": str(uuid.uuid4())}, **kwargs)


def test_the_owner_issues_both_kinds_of_token_and_uses_them_on_localhost(owner):
    mine = write(
        owner,
        "post",
        "/v1/me/agent-tokens",
        json={"client_name": "My agent", "scopes": ["matches:read"], "consent_text_version": "agent-access-2026-11"},
    )
    assert mine.status_code == 201, mine.text
    org = write(owner, "post", "/v1/orgs", json={"name": "Owner Demo", "timezone": "UTC"})
    assert org.status_code == 201, org.text
    theirs = write(
        owner,
        "post",
        f"/v1/orgs/{org.json()['data']['id']}/agent-tokens",
        json={"client_name": "My hiring agent", "scopes": ["twin:ask", "introductions:request"]},
    )
    assert theirs.status_code == 201, theirs.text

    candidate_tools = rpc(owner, mine.json()["data"]["token"], "tools/list").json()["result"]["tools"]
    employer_tools = rpc(owner, theirs.json()["data"]["token"], "tools/list").json()["result"]["tools"]
    searched = call(owner, mine.json()["data"]["token"], "search_matches")

    assert [t["name"] for t in candidate_tools] == ["search_matches"]
    assert [t["name"] for t in employer_tools] == [
        "list_introductions",
        "ask_twin",
        "get_twin_answer",
        "request_introduction",
    ]
    assert searched.status_code == 200 and searched.json()["result"]["structuredContent"]["matches"] == []


def test_a_rebound_domain_reaches_neither_the_local_routes_nor_mcp(owner):
    token = write(
        owner,
        "post",
        "/v1/me/agent-tokens",
        json={"client_name": "My agent", "scopes": ["matches:read"], "consent_text_version": "agent-access-2026-11"},
    ).json()["data"]["token"]

    rebound = {"Host": "attacker.example:8080", "Origin": "http://attacker.example:8080"}

    assert rpc(owner, token, "tools/list", headers={"Host": rebound["Host"]}).status_code == 421
    assert rpc(owner, token, "tools/list", headers={"Origin": rebound["Origin"]}).status_code == 403
    assert owner.get("/v1/local/status", headers={"Host": rebound["Host"]}).status_code == 403  # the local guard itself
    assert rpc(owner, token, "tools/list").status_code == 200


def test_the_mcp_origins_setting_does_not_weaken_the_local_mode_settings():
    # local mode still refuses a base URL that is not on this computer, and a remote MCP origin still needs the flag
    with pytest.raises(config.ConfigError):
        config.load({"APP_MODE": "local", "APP_BASE_URL": "https://careers.example"})
    with pytest.raises(config.ConfigError):
        config.load({"APP_MODE": "local", "MCP_ALLOWED_ORIGINS": "https://careers.example"})
    assert config.load({"APP_MODE": "local"}).mcp_allowed_origins == ("http://127.0.0.1:8080", "http://localhost:8080")
