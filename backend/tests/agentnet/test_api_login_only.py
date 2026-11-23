"""The API process holds only the API login (plan 09-04; T-09-30 and the Phase 6 integration fix, PR #4).

The worker's login serves every person (its policies are USING (true)) and the match module's reads every candidate, so
neither may exist in the API container. Here their URLs point at nothing: if a route or an MCP tool opened either, it
would fail the way it did on the Compose stack. Every tool is called over /mcp, the token routes over HTTP, and the
engines
the process made are listed. A second test reads the source of every module a route or tool reaches. Everything is
invented.
"""

import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.platform import config, db
from app.platform.sessions import Principal, current_user
from tests.agentnet.factories import call, candidate_token, org_token, shown_matches, twin_world
from tests.conftest import _empty_tables

UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
APP = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def api_login_only(monkeypatch, engine):
    _empty_tables(engine)
    current = config.get()
    monkeypatch.setattr(
        config, "get", lambda: replace(current, database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    )
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make them again
    yield
    for made in db._engines.values():
        made.dispose()
    _empty_tables(engine)


def test_every_tool_and_every_token_route_runs_with_the_worker_and_match_urls_unreachable(
    engine, api_login_only, monkeypatch
):
    from app.twin import api as twin_api

    monkeypatch.setattr(twin_api, "ANSWER_WAIT_SECONDS", 0)
    with engine.begin() as conn:
        world = twin_world(conn)
        mine = candidate_token(conn, world.candidate.id)
        theirs = org_token(conn, world.org, world.org.owner)
        matches = shown_matches(conn, world.candidate)
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        calls = [
            (mine.secret, "search_matches", {}),
            (mine.secret, "explain_match", {"decision_id": str(matches.first)}),
            (theirs.secret, "list_introductions", {}),
            (
                theirs.secret,
                "ask_twin",
                {"introduction_id": str(world.introduction), "question": "What did you build?"},
            ),
            (theirs.secret, "get_twin_answer", {"question_id": str(uuid4())}),
            (theirs.secret, "request_introduction", {"role_id": str(world.role.id)}),
            (mine.secret, "ask_twin", {}),  # refused at the gate: the refusal row is written on the API login too
        ]
        codes = [call(client, secret, tool, arguments).status_code for secret, tool, arguments in calls]
        app.dependency_overrides[current_user] = lambda: Principal(world.candidate.id)
        issued = client.post(
            "/v1/me/agent-tokens",
            json={"client_name": "Demo", "scopes": ["matches:read"], "consent_text_version": "agent-access-2026-11"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        listed = client.get("/v1/me/agent-tokens")
        revoked = client.delete(
            f"/v1/me/agent-tokens/{issued.json()['data']['id']}", headers={"Idempotency-Key": str(uuid4())}
        )

    assert codes == [200, 200, 200, 200, 200, 200, 403]
    assert (issued.status_code, listed.status_code, revoked.status_code) == (201, 200, 204)
    assert set(db._engines) == {"api"}


ROUTE_REACHABLE = [
    *sorted((APP / "agentnet").glob("*.py")),
    APP / "hiring" / "api.py",
    APP / "match" / "agent_reads.py",
    APP / "twin" / "api.py",
]


def test_no_route_or_tool_module_opens_a_worker_or_match_transaction():
    pattern = re.compile(r"""role\s*=\s*["'](worker|match|migrate)["']|engine\(\s*["'](worker|match|migrate)["']""")
    offenders = [str(path.relative_to(APP)) for path in ROUTE_REACHABLE if pattern.search(path.read_text())]

    assert offenders == []
    assert any(path.name == "server.py" for path in ROUTE_REACHABLE)  # the list is not empty by accident
