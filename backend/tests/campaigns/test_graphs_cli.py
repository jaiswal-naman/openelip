"""`graphs check` and `graphs reseat` (plan 07-04; ARCHITECTURE 5.9 rule 9): threads parked before a node the graph no
longer has are found, and reset so that `route` places each campaign again from its tables. The "old" graph is the
campaign graph with its park node under another name, the rename the rule warns about."""

from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine

from app import cli
from app.campaigns import graph, nodes
from app.guardian import runtime
from app.platform.llm import fake
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401


@pytest.fixture(autouse=True)
def graph_world(clean, monkeypatch):  # noqa: F811
    runtime.close()
    w.use_fakes(monkeypatch)
    yield
    runtime.close()


def old_graph() -> Any:
    """The campaign graph as it was when the park node was called `legacy_park`."""
    builder = StateGraph(nodes.State)
    builder.add_node("route", nodes.route)
    for name, fn in graph.WORK.items():
        builder.add_node(name, fn)
    builder.add_node("legacy_park", runtime.park_node(nodes.describe))
    builder.add_edge(START, "route")
    targets = {name: name for name in graph.WORK} | {"park": "legacy_park", nodes.END: END}
    builder.add_conditional_edges("route", lambda state: state["next"] or "park", targets)
    for name in (*graph.WORK, "legacy_park"):
        builder.add_edge(name, "route")
    return builder.compile(checkpointer=runtime.saver())


def park_under(old: bool, world: w.World) -> None:
    fake.replay_from(None)  # each campaign reads the recordings from their start
    thread = nodes.thread_id(world.campaign)
    compiled = old_graph() if old else graph.compiled()
    runtime.drive(thread, compiled, cause="start", start_state=graph.start_state(world.campaign))
    assert runtime.thread_state(compiled, thread)[0] == "parked"


def test_check_finds_nothing_for_a_thread_on_the_current_graph(engine: Engine, capsys) -> None:
    world = w.build_world(engine)
    park_under(False, world)
    assert cli.main(["graphs", "check"]) == 0
    assert "0 thread(s)" in capsys.readouterr().out


def test_check_lists_a_thread_parked_before_a_node_that_no_longer_exists_and_exits_1(engine: Engine, capsys) -> None:
    stuck, fine = w.build_world(engine), w.build_world(engine)
    park_under(True, stuck)
    park_under(False, fine)
    assert cli.main(["graphs", "check"]) == 1
    out = capsys.readouterr().out
    assert f"campaign:{stuck.campaign} parked before legacy_park" in out and str(fine.campaign) not in out
    assert graph.parked_on_missing_nodes(runtime.saver(), graph.compiled()) == [f"campaign:{stuck.campaign}"]


def test_reseat_deletes_the_checkpoints_and_the_campaign_is_placed_again_from_its_tables(
    engine: Engine, capsys
) -> None:
    stuck = w.build_world(engine)
    park_under(True, stuck)
    before = w.rows(engine, "SELECT id, status::text AS status FROM approvals")
    assert cli.main(["graphs", "reseat", "--node", "legacy_park"]) == 0
    assert "1 thread(s) reset, 1 campaign(s) asked" in capsys.readouterr().out
    assert (
        w.one(
            engine, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t", t=nodes.thread_id(stuck.campaign)
        )
        == 0
    )
    (job,) = w.rows(engine, "SELECT payload FROM queue_jobs WHERE kind = 'campaign.resume'")
    assert job.payload["cause"].startswith("reseat.")
    w.run_jobs()
    assert runtime.thread_state(graph.compiled(), nodes.thread_id(stuck.campaign))[0] == "parked"
    assert cli.main(["graphs", "check"]) == 0
    # approvals and steps survived as rows, and nothing was proposed twice
    assert w.rows(engine, "SELECT id, status::text AS status FROM approvals") == before
    assert w.one(engine, "SELECT count(*) FROM agent_actions WHERE kind = 'outside_action'") == 1
    assert w.stage(engine, stuck) == "needs_you"


def test_reseat_leaves_threads_parked_before_other_nodes_alone(engine: Engine, capsys) -> None:
    stuck = w.build_world(engine)
    park_under(True, stuck)
    assert cli.main(["graphs", "reseat", "--node", "some_other_node"]) == 0
    assert "0 thread(s) reset" in capsys.readouterr().out
    assert (
        w.one(
            engine, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t", t=nodes.thread_id(stuck.campaign)
        )
        > 0
    )
