"""The agent runtime (plan 07-04): the saver pool, the start-up check, `park` and `drive` on a toy graph that has the
same
shape as the campaign graph (a node, a park, a node) and writes its checkpoints to schema `langgraph` as
`app_worker`."""

import uuid
from collections.abc import Iterator
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import Engine, text

from app.guardian import runtime


class Toy(TypedDict):
    campaign_id: str
    next: str | None
    cause: str | None


@pytest.fixture(autouse=True)
def pool_for_this_database(clean) -> Iterator[None]:
    runtime.close()
    yield
    runtime.close()


def toy_graph(log: list[str], *, fail_once: bool = False) -> Any:
    failed = {"done": not fail_once}

    def first(state: Toy) -> dict[str, Any]:
        log.append("first")
        if not failed["done"]:
            failed["done"] = True
            raise RuntimeError("a node died")
        return {"next": None}

    def last(state: Toy) -> dict[str, Any]:
        log.append(f"last:{state['cause']}")
        return {"next": None}

    graph = StateGraph(Toy)
    graph.add_node("first", first)
    graph.add_node("park", runtime.park_node(lambda campaign_id: {"waiting_for": "event", "ref": campaign_id}))
    graph.add_node("last", last)
    graph.add_edge(START, "first")
    graph.add_edge("first", "park")
    graph.add_edge("park", "last")
    graph.add_edge("last", END)
    return graph.compile(checkpointer=runtime.saver())


def start(thread: str) -> dict[str, Any]:
    return {"campaign_id": thread, "next": None, "cause": "start"}


def test_checkpoints_are_current_on_a_migrated_database() -> None:
    runtime.assert_checkpoints_current()


def test_a_behind_checkpoint_schema_stops_the_worker(engine: Engine) -> None:
    with engine.begin() as c:
        newest = c.execute(text("SELECT max(v) FROM langgraph.checkpoint_migrations")).scalar_one()
        c.execute(text("DELETE FROM langgraph.checkpoint_migrations WHERE v = :v"), {"v": newest})
    try:
        with pytest.raises(SystemExit, match="behind the installed library"):
            runtime.assert_checkpoints_current()
    finally:
        with engine.begin() as c:
            c.execute(text("INSERT INTO langgraph.checkpoint_migrations (v) VALUES (:v)"), {"v": newest})
    runtime.assert_checkpoints_current()


def test_a_missing_middle_migration_is_caught_too(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM langgraph.checkpoint_migrations WHERE v = 0"))
    try:
        with pytest.raises(SystemExit):
            runtime.assert_checkpoints_current()
    finally:
        with engine.begin() as c:
            c.execute(text("INSERT INTO langgraph.checkpoint_migrations (v) VALUES (0)"))


def test_start_park_resume_and_end() -> None:
    log: list[str] = []
    graph = toy_graph(log)
    thread = f"campaign:{uuid.uuid4()}"
    assert runtime.drive(thread, graph, cause="start", start_state=start(thread)) == "started"
    assert log == ["first"]
    state, snapshot = runtime.thread_state(graph, thread)
    assert state == "parked"
    assert snapshot.interrupts[0].value["waiting_for"] == "event"
    # a resume carries the cause only; the node after the park reads it from the state
    assert runtime.drive(thread, graph, cause="approval.7", start_state=start(thread)) == "resumed"
    assert log == ["first", "last:approval.7"]
    assert runtime.thread_state(graph, thread)[0] == "ended"
    assert runtime.drive(thread, graph, cause="late", start_state=start(thread)) == "ended"
    assert log == ["first", "last:approval.7"]


def test_a_missing_thread_without_a_start_state_is_left_alone() -> None:
    graph = toy_graph([])
    assert runtime.drive(f"campaign:{uuid.uuid4()}", graph, cause="x", start_state=None) == "missing"


def test_a_crashed_thread_continues_from_its_checkpoint() -> None:
    log: list[str] = []
    graph = toy_graph(log, fail_once=True)
    thread = f"campaign:{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        runtime.drive(thread, graph, cause="start", start_state=start(thread))
    assert runtime.thread_state(graph, thread)[0] == "crashed"
    assert runtime.drive(thread, graph, cause="again", start_state=start(thread)) == "continued"
    assert log == ["first", "first"]  # the node that died ran again, from its start
    assert runtime.thread_state(graph, thread)[0] == "parked"


def test_checkpoints_land_in_schema_langgraph_as_the_worker(engine: Engine) -> None:
    graph = toy_graph([])
    thread = f"campaign:{uuid.uuid4()}"
    runtime.drive(thread, graph, cause="start", start_state=start(thread))
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t"), {"t": thread}
        ).scalar_one()
        assert rows > 0
        assert c.execute(text("SELECT count(*) FROM public.queue_jobs")).scalar_one() == 0
    with runtime.pool().connection() as conn:
        who = conn.execute("SELECT current_user AS u, current_schema() AS s").fetchone()
    assert who == {"u": "app_worker", "s": "langgraph"}


def test_a_second_session_on_the_same_thread_is_busy(engine: Engine) -> None:
    graph = toy_graph([])
    thread = f"campaign:{uuid.uuid4()}"
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:t, 0))"), {"t": thread})
        with pytest.raises(runtime.ThreadBusy):
            runtime.drive(thread, graph, cause="start", start_state=start(thread))
        holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:t, 0))"), {"t": thread})
    assert runtime.drive(thread, graph, cause="start", start_state=start(thread)) == "started"


def test_the_lock_is_released_when_a_run_fails() -> None:
    graph = toy_graph([], fail_once=True)
    thread = f"campaign:{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        runtime.drive(thread, graph, cause="start", start_state=start(thread))
    assert runtime.drive(thread, graph, cause="again", start_state=None) == "continued"


def test_checkpoint_rows_hold_ids_and_the_cause_never_text(engine: Engine) -> None:
    canary = "CANARY-7f3a-not-for-a-checkpoint"
    seen: list[str] = []

    def work(state: Toy) -> dict[str, Any]:
        seen.append(canary)  # the text is used in the node, and kept out of what it returns
        return {"next": None}

    graph = StateGraph(Toy)
    graph.add_node("work", work)
    graph.add_node("park", runtime.park_node(lambda campaign_id: {"waiting_for": "event", "ref": campaign_id}))
    graph.add_edge(START, "work")
    graph.add_edge("work", "park")
    graph.add_edge("park", END)
    compiled = graph.compile(checkpointer=runtime.saver())
    thread = f"campaign:{uuid.uuid4()}"
    runtime.drive(thread, compiled, cause="start", start_state=start(thread))
    runtime.drive(thread, compiled, cause="done.1", start_state=None)
    assert seen == [canary]
    with engine.connect() as c:
        everything = []
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            rows = c.execute(text(f"SELECT t::text FROM langgraph.{table} t")).scalars().all()  # noqa: S608
            everything += rows
    assert everything and not any(canary in row for row in everything)
