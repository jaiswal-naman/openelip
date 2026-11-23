"""The campaign graph (ARCHITECTURE 5.9; AI_SYSTEM 6.2): one durable LangGraph thread per campaign, `campaign:{id}`.

Every work node and `park` returns to `route`, which reads the tables and names the next node, so a fresh thread for an
open campaign lands where the tables say. The state is three short strings (`campaign_id`, `next`, `cause`) and nothing
else, so a checkpoint holds no resume, chat, contact, dossier or job text.

`GRAPH_VERSION` is written to `agent_runs.config_version`. A node that is renamed or removed leaves threads parked
before it unable to continue: `parked_on_missing_nodes` finds them (`python -m app.cli graphs check`) and `reseat`
deletes their checkpoints so `route` places each campaign again from its tables (`graphs reseat`).
"""

from collections.abc import Hashable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text

from app.guardian import runtime
from app.platform import db, queue

from . import nodes, stages
from .nodes import State

GRAPH_VERSION = "campaign-graph-1"
RESUME_ATTEMPTS = stages.RESUME_ATTEMPTS


WORK = {
    "research": nodes.research_node,
    "fit": nodes.fit_node,
    "packet_draft": nodes.packet_draft_node,
    "packet": nodes.packet_node,
    "follow_up": nodes.follow_up_node,
    "interview_prep": nodes.interview_prep_node,
}


def build(checkpointer: PostgresSaver | None) -> CompiledStateGraph[State, Any, State, State]:
    """The graph: START, `route`; route names a work node, `park` or the end; each returns to `route`."""
    graph = StateGraph(State)
    graph.add_node("route", nodes.route)
    for name, fn in WORK.items():
        graph.add_node(name, fn)
    graph.add_node("park", runtime.park_node(nodes.describe))  # type: ignore[arg-type]
    graph.add_edge(START, "route")
    targets: dict[Hashable, str] = {name: name for name in (*WORK, "park")}
    targets[nodes.END] = END
    graph.add_conditional_edges("route", lambda state: state["next"] or "park", targets)
    for name in (*WORK, "park"):
        graph.add_edge(name, "route")
    return graph.compile(checkpointer=checkpointer)


_compiled: tuple[PostgresSaver, CompiledStateGraph[State, Any, State, State]] | None = None


def compiled() -> CompiledStateGraph[State, Any, State, State]:
    """The graph on the worker's saver, built once per saver."""
    global _compiled
    saver = runtime.saver()
    if _compiled is None or _compiled[0] is not saver:
        _compiled = (saver, build(saver))
    return _compiled[1]


def start_state(campaign_id: UUID | str) -> dict[str, Any]:
    return {"campaign_id": str(campaign_id), "next": None, "cause": "start"}


# ---- graph changes in flight (rule 9) ----------

_TRIGGER = "branch:to:"


def missing_nodes_by_thread(saver: PostgresSaver, graph: CompiledStateGraph[Any, Any, Any, Any]) -> dict[str, str]:
    """{thread id: the node it is waiting to enter} for every thread whose newest checkpoint is about to enter a node
    the graph no longer has: the node's trigger channel has a version the node has not seen."""
    with runtime.pool().connection() as conn:
        threads = [row["thread_id"] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]
    missing: dict[str, str] = {}
    for thread in sorted(threads):
        found = saver.get_tuple({"configurable": {"thread_id": thread}})
        if found is None:
            continue
        versions = found.checkpoint["channel_versions"]
        seen = found.checkpoint["versions_seen"]
        for channel, version in versions.items():
            node = channel.removeprefix(_TRIGGER)
            if channel.startswith(_TRIGGER) and node not in graph.nodes and seen.get(node, {}).get(channel) != version:
                missing[thread] = node
                break
    return missing


def parked_on_missing_nodes(saver: PostgresSaver, graph: CompiledStateGraph[Any, Any, Any, Any]) -> list[str]:
    """Thread ids parked before a node the graph no longer has. Empty when every thread can continue."""
    return list(missing_nodes_by_thread(saver, graph))


def reseat(saver: PostgresSaver, thread_ids: Sequence[str]) -> int:
    """Delete these threads' checkpoints and ask each open campaign's new thread to start (`campaign.resume`, cause
    `reseat.{timestamp}`); `route` places it from the tables. Returns how many campaigns were asked."""
    cause = f"reseat.{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
    asked = 0
    for thread in thread_ids:
        saver.delete_thread(thread)
        campaign_id = UUID(thread.removeprefix("campaign:"))
        with db.transaction(role="worker") as conn:
            owner = conn.execute(
                text("SELECT user_id FROM campaigns WHERE id = :id AND stage <> 'closed'"), {"id": campaign_id}
            ).scalar_one_or_none()
            if owner is None:
                continue
            queue.enqueue(
                conn,
                "campaign.resume",
                {"campaign_id": str(campaign_id), "cause": cause},
                unique_key=f"campaign.resume:{campaign_id}:{cause}",
                max_attempts=RESUME_ATTEMPTS,
                user_id=owner,
            )
            asked += 1
    return asked
