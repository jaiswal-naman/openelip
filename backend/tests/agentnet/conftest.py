"""Fixtures of the agent-network tests: the whole app with its lifespan, served in process on a loopback address."""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.agentnet.factories import candidate_token, org_token, shown_matches, twin_world
from tests.conftest import _empty_tables


@pytest.fixture
def mcp(engine) -> Iterator[SimpleNamespace]:
    """`TestClient(app.main.app, base_url="http://127.0.0.1:8080")`: the SDK's host check answers 421 to the default
    `Host: testserver`. The lifespan runs (the SDK's session manager must be entered), no socket is opened, and the
    tables the API commits to are emptied before and after."""
    _empty_tables(engine)
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield SimpleNamespace(client=client, app=app, engine=engine, limiter=app.state.mcp.limiter)
    _empty_tables(engine)


@pytest.fixture
def agents(mcp) -> SimpleNamespace:
    """A world (organization, owner, member, open role, candidate with an accepted introduction), a candidate's token
    with both scopes, an organization token with both scopes and shown matches; everything committed."""
    with mcp.engine.begin() as conn:
        world = twin_world(conn)
        mcp.candidate_token = candidate_token(conn, world.candidate.id)
        mcp.org_token = org_token(conn, world.org, world.org.owner)
        mcp.matches = shown_matches(conn, world.candidate)
    mcp.world = world
    return mcp
