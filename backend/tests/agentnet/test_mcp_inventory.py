"""The tool catalogue against its snapshot (plan 09-04 task 3; API 5.4): names, titles, descriptions, input and output
schemas and annotations of `tools/list` for each kind of token equal `fixtures/tools_list.json`, which was written
from the
specification and is not a recording of the server. Everything here is invented.
"""

import json
from pathlib import Path

import pytest

from app.agentnet import server
from tests.agentnet.factories import candidate_token, org_token, rpc

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tools_list.json").read_text())
SPEC = {tool["name"]: tool for tool in FIXTURE["tools"]}
KEYS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def token_of(agents, kind):
    scopes = {
        "candidate_both_scopes": ("matches:read", "explanations:read"),
        "candidate_matches_only": ("matches:read",),
        "candidate_explanations_only": ("explanations:read",),
        "org_both_scopes": ("twin:ask", "introductions:request"),
        "org_twin_only": ("twin:ask",),
        "org_introductions_only": ("introductions:request",),
    }[kind]
    with agents.engine.begin() as conn:
        if kind.startswith("candidate"):
            return candidate_token(conn, agents.world.candidate.id, scopes=scopes)
        return org_token(conn, agents.world.org, agents.world.org.owner, scopes=scopes)


@pytest.mark.parametrize("kind", sorted(FIXTURE["visible"]))
def test_each_kind_of_token_lists_exactly_its_tools_as_the_snapshot_says(agents, kind):
    issued = token_of(agents, kind)

    listed = rpc(agents.client, issued.secret, "tools/list").json()["result"]["tools"]

    assert [tool["name"] for tool in listed] == FIXTURE["visible"][kind]
    for tool in listed:
        assert {key: tool.get(key) for key in KEYS} == {key: SPEC[tool["name"]].get(key) for key in KEYS}


def test_the_catalogue_in_code_is_the_snapshot_and_the_fixed_order(agents):
    assert [tool.name for tool in server.TOOLS] == [tool["name"] for tool in FIXTURE["tools"]]
    assert [tool.name for tool in server.TOOLS] == [
        "search_matches",
        "explain_match",
        "list_introductions",
        "ask_twin",
        "get_twin_answer",
        "request_introduction",
    ]


def test_every_tool_description_names_third_party_text_as_data_where_it_returns_some(agents):
    for tool in FIXTURE["tools"]:
        if tool["name"] in ("request_introduction",):
            continue  # returns a count and a fixed note only
        assert "data" in tool["description"], tool["name"]


def test_the_input_models_and_the_published_input_schemas_agree(agents):
    for tool in server.TOOLS:
        generated = tool.model.model_json_schema()
        published = tool.input_schema
        assert set(generated["properties"]) == set(published["properties"]), tool.name
        assert set(generated.get("required", [])) == set(published.get("required", [])), tool.name
        assert generated.get("additionalProperties") is False and published["additionalProperties"] is False


def test_a_token_that_holds_no_scope_a_tool_needs_lists_nothing_of_it(agents):
    # the two catalogues never mix: a candidate token has no organization tool and the reverse
    issued = token_of(agents, "candidate_both_scopes")
    listed = rpc(agents.client, issued.secret, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listed}.isdisjoint(
        {"list_introductions", "ask_twin", "get_twin_answer", "request_introduction"}
    )
