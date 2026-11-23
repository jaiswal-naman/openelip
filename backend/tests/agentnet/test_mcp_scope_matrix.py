"""The scope matrix (plan 09-04 task 3; NET-03; T-09-27): every tool called with every kind of token. A tool outside the
token's scopes is 403 `insufficient_scope`, writes one refusal row and is never executed; a tool inside them is served.
Everything here is invented.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.agentnet.server import SCOPE_OF
from tests.agentnet.factories import call, candidate_token, org_token
from tests.hiring.factories import make_candidate

KINDS = {
    "candidate_both": ("candidate", ("matches:read", "explanations:read")),
    "candidate_matches": ("candidate", ("matches:read",)),
    "candidate_explanations": ("candidate", ("explanations:read",)),
    "org_both": ("organization", ("twin:ask", "introductions:request")),
    "org_twin": ("organization", ("twin:ask",)),
    "org_introductions": ("organization", ("introductions:request",)),
}


def arguments(agents, tool):
    return {
        "search_matches": {},
        "explain_match": {"decision_id": str(agents.matches.first)},
        "list_introductions": {},
        "ask_twin": {
            "introduction_id": str(agents.world.introduction),
            "question": "What did you build at your last job?",
        },
        "get_twin_answer": {"question_id": str(uuid4())},
        "request_introduction": {"role_id": str(agents.world.role.id)},
    }[tool]


def run(engine, sql, **params):
    with engine.begin() as conn:
        return conn.execute(text(sql), params).all()


@pytest.fixture(autouse=True)
def no_twin_wait(monkeypatch):
    from app.twin import api

    monkeypatch.setattr(api, "ANSWER_WAIT_SECONDS", 0)


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_tool_with_this_kind_of_token(agents, kind):
    principal, scopes = KINDS[kind]
    with agents.engine.begin() as conn:
        issued = (
            candidate_token(conn, agents.world.candidate.id, scopes=scopes)
            if principal == "candidate"
            else org_token(conn, agents.world.org, agents.world.org.owner, scopes=scopes)
        )

    served, refused = [], []
    for tool, needed in SCOPE_OF.items():
        response = call(agents.client, issued.secret, tool, arguments(agents, tool))
        allowed = bool(set(needed) & set(scopes))
        if allowed:
            assert response.status_code == 200, (tool, response.text)
            served.append(tool)
        else:
            assert response.status_code == 403, (tool, response.status_code)
            wanted = response.headers["www-authenticate"]
            assert wanted == f'Bearer error="insufficient_scope", scope="{needed[0]}"'
            refused.append(tool)

    rows = run(
        agents.engine,
        """SELECT details->>'tool', details->>'reason' FROM audit_log WHERE agent_token_id = :t
            AND event_type = 'agentnet.agent_call_refused' AND details->>'reason' = 'insufficient_scope'""",
        t=issued.id,
    )
    assert sorted(r[0] for r in rows) == sorted(refused)
    # an organization token is never a candidate's and the reverse: no tool is served to both kinds
    if principal == "candidate":
        assert set(served) <= {"search_matches", "explain_match"}
    else:
        assert set(served) <= {"list_introductions", "ask_twin", "get_twin_answer", "request_introduction"}


def test_a_refused_call_is_never_executed(agents):
    with agents.engine.begin() as conn:
        narrow = org_token(conn, agents.world.org, agents.world.org.owner, scopes=("introductions:request",))
        reader = candidate_token(conn, agents.world.candidate.id, scopes=("matches:read",))

    ask = call(agents.client, narrow.secret, "ask_twin", arguments(agents, "ask_twin"))  # needs twin:ask
    explain = call(agents.client, reader.secret, "explain_match", arguments(agents, "explain_match"))

    assert (ask.status_code, explain.status_code) == (403, 403)
    assert run(agents.engine, "SELECT count(*) FROM twin_messages")[0][0] == 0  # no question was stored
    assert run(agents.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'twin.resume'")[0][0] == 0
    calls = run(agents.engine, "SELECT count(*) FROM audit_log WHERE event_type = 'agentnet.agent_call'")[0][0]
    assert calls == 0  # nothing was recorded as an allowed call


def test_a_candidate_token_never_reads_another_candidates_decisions(agents):
    with agents.engine.begin() as conn:
        stranger = make_candidate(conn)
        mine = candidate_token(conn, stranger.id)  # a different candidate's token, with no matches of their own

    found = call(agents.client, mine.secret, "search_matches")
    explained = call(agents.client, mine.secret, "explain_match", {"decision_id": str(agents.matches.first)})

    assert json.loads(found.json()["result"]["content"][0]["text"])["matches"] == []
    assert explained.json()["result"]["structuredContent"]["refusal"]["code"] == "not_found"


def test_an_organization_token_reaches_no_candidate_data_beyond_its_accepted_introductions(agents):
    secret = agents.org_token.secret
    response = call(agents.client, secret, "list_introductions")
    body = json.dumps(response.json())

    assert str(agents.world.candidate.id) not in body  # no user id of the candidate is ever returned
    assert agents.world.candidate.email not in body
