"""The Twin graph (ARCHITECTURE 5.9; AI_SYSTEM 8.3): one durable LangGraph thread per employer question,
`twin:{question_id}`.

    triage -> draft -> park -> finish -> end          (draft may also end, finish may wait again)
    triage -> ask_candidate -> propose -> park        (the record does not answer, or the dial is never)

The state is four short values (question id, next node, resume cause, parked action): no question, fact, draft or
reply text, so a checkpoint holds nothing about the candidate. `GRAPH_VERSION` is written to
`agent_runs.config_version`. A node that is renamed or removed leaves threads parked before it unable to continue:
`missing_nodes_by_thread` finds them (`python -m app.cli graphs check`) and `reseat` deletes their checkpoints and
starts each open question again (every node checks the tables first, so starting again from `triage` is safe).
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

from . import deliver, nodes
from .api import QUEUE, RESUME_ATTEMPTS
from .jobs import RESUME
from .nodes import GRAPH_VERSION, State

__all__ = ["GRAPH_VERSION", "build", "compiled", "missing_nodes_by_thread", "reseat", "start_state", "thread_id"]

thread_id = deliver.thread_id
WORK = {
    "triage": nodes.triage,
    "draft": nodes.draft,
    "ask_candidate": nodes.ask_candidate,
    "propose": nodes.propose,
    "park": nodes.park,
    "finish": nodes.finish,
}
# where each node may go next: the names its `next` can take
EXITS = {
    "triage": ("draft", "ask_candidate"),
    "draft": ("ask_candidate", "park"),
    "propose": ("park",),
    "finish": ("park",),
}


def build(checkpointer: PostgresSaver | None) -> CompiledStateGraph[State, Any, State, State]:
    graph = StateGraph(State)
    for name, fn in WORK.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "triage")
    for name, exits in EXITS.items():
        targets: dict[Hashable, str] = {target: target for target in exits}
        targets[nodes.END] = END
        graph.add_conditional_edges(name, lambda state: state["next"] or nodes.END, targets)
    graph.add_edge("ask_candidate", "propose")
    graph.add_edge("park", "finish")
    return graph.compile(checkpointer=checkpointer)


_compiled: tuple[PostgresSaver, CompiledStateGraph[State, Any, State, State]] | None = None


def compiled() -> CompiledStateGraph[State, Any, State, State]:
    """The graph on the worker's saver, built once per saver."""
    global _compiled
    saver = runtime.saver()
    if _compiled is None or _compiled[0] is not saver:
        _compiled = (saver, build(saver))
    return _compiled[1]


def start_state(question_id: UUID | str) -> dict[str, Any]:
    return {"question_id": str(question_id), "next": None, "cause": "start", "parked_on": None}


# ---- graph changes in flight (ARCHITECTURE 5.9 rule 9) ----------

_TRIGGER = "branch:to:"
PREFIX = "twin:"


def missing_nodes_by_thread(saver: PostgresSaver, graph: CompiledStateGraph[Any, Any, Any, Any]) -> dict[str, str]:
    """{thread id: the node it is waiting to enter} for every Twin thread whose newest checkpoint is about to enter a
    node this graph no longer has: the node's trigger channel has a version the node has not seen."""
    with runtime.pool().connection() as conn:
        threads = [
            row["thread_id"]
            for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'twin:%'")
        ]
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


def reseat(saver: PostgresSaver, thread_ids: Sequence[str]) -> int:
    """Delete these threads' checkpoints and ask each unanswered question's new thread to start (`twin.resume`, cause
    `reseat.{timestamp}`). Returns how many questions were asked."""
    cause = f"reseat.{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
    asked = 0
    for thread in thread_ids:
        saver.delete_thread(thread)
        question_id = UUID(thread.removeprefix(PREFIX))
        with db.transaction(role="worker") as conn:
            owner = conn.execute(
                text("SELECT m.user_id FROM twin_messages m WHERE m.id = :q AND m.author = 'employer'"),
                {"q": question_id},
            ).scalar_one_or_none()
            if owner is None or deliver.has_final(conn, question_id):
                continue
            queue.enqueue(
                conn,
                RESUME,
                {"question_id": str(question_id), "cause": cause},
                unique_key=f"{RESUME}:{question_id}:{cause}",
                queue=QUEUE,
                max_attempts=RESUME_ATTEMPTS,
                user_id=owner,
            )
            asked += 1
    return asked
