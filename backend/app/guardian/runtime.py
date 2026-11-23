"""The agent runtime: where a graph thread lives and how a queue job drives it (ARCHITECTURE 5.9; INDEX "Checkpointer").

This is the only file of the Guardian that imports `langgraph` or `psycopg_pool`. It holds
  * the saver, on a pool of its own (`autocommit=True`, `dict_row`, `search_path=langgraph`) logged in as `app_worker`;
  * `assert_checkpoints_current`, which makes the worker refuse to start when the library's migrations are not all
    recorded (the schema is built by Alembic, never by the library at run time);
  * `park_node`, the one node that calls `interrupt` (rule 6); and
  * `drive`, which a queue job calls to start, resume or continue one thread under a session advisory lock (rule 7).

Checkpoints hold ids, the next node and the cause, never text (rule 2): the state is whatever the graph's TypedDict
says, and the campaign graph's holds three short strings.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import text

from app.platform import config, db

POOL_MAX = 4  # assumption: one thread runs at a time per worker; the rest is headroom for delete and check commands

_pool: ConnectionPool[Any] | None = None
_saver: PostgresSaver | None = None


class ThreadBusy(Exception):
    """Another session holds the thread's lock. The queue retries the job on its first backoff (INDEX decision 25)."""


def _conninfo() -> str:
    url = config.get().database_url("worker")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def pool() -> ConnectionPool[Any]:
    """The saver's pool, opened on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _conninfo(),
            min_size=1,
            max_size=POOL_MAX,
            kwargs={"autocommit": True, "row_factory": dict_row, "options": "-c search_path=langgraph"},
            open=True,
        )
    return _pool


def saver() -> PostgresSaver:
    global _saver
    if _saver is None:
        _saver = PostgresSaver(pool())
    return _saver


def close() -> None:
    """Close the pool (tests, and a worker that stops)."""
    global _pool, _saver
    if _pool is not None:
        _pool.close()
    _pool = None
    _saver = None


def assert_checkpoints_current() -> None:
    """Exit with a clear message unless every migration of the installed saver is recorded in
    `langgraph.checkpoint_migrations`. The worker calls it before its loop."""
    wanted = len(PostgresSaver.MIGRATIONS)
    try:
        with pool().connection() as conn:
            row = conn.execute("SELECT count(*) AS n, max(v) AS newest FROM checkpoint_migrations").fetchone()
    except Exception as error:  # a missing schema or table is the same problem as a behind one
        raise SystemExit(
            f"the LangGraph checkpoint tables cannot be read ({type(error).__name__}): run the Alembic migrations"
        ) from None
    have = int(row["n"]) if row else 0
    newest = -1 if row is None or row["newest"] is None else int(row["newest"])
    if have != wanted or newest != wanted - 1:
        raise SystemExit(
            f"the LangGraph checkpoint schema is behind the installed library ({have} of {wanted} migrations "
            "recorded): run the Alembic migrations before starting the worker"
        )


def park_node(describe: Callable[[str], dict[str, Any]]) -> Callable[[Any], dict[str, Any]]:
    """The node that waits. `describe(campaign_id)` says what it waits for: {"waiting_for": "approval" | "time" |
    "event", "ref": <id>}. A resume (`Command(resume={"cause": ...})`) returns the cause into the state and the graph
    goes back to `route`, which parks again if nothing changed."""

    def park(state: Any) -> dict[str, Any]:
        value = interrupt(describe(state["campaign_id"]))
        return {"cause": str((value or {}).get("cause")), "next": None}

    return park


@contextmanager
def thread_lock(thread_id: str) -> Iterator[None]:
    """A session advisory lock on the thread id, held on a connection of its own for the whole run. Raises ThreadBusy
    when another session holds it. A killed process loses its connection, and the lock with it."""
    with db.engine("worker").connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        taken = conn.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:t, 0))"), {"t": thread_id}
        ).scalar_one()
        if not taken:
            raise ThreadBusy(thread_id)
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:t, 0))"), {"t": thread_id})


def thread_state(graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str) -> tuple[str, Any]:
    """`missing`, `parked`, `crashed` (stopped inside a run) or `ended`, with the snapshot."""
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    if snapshot.next:
        return ("parked" if snapshot.interrupts else "crashed"), snapshot
    return ("ended" if snapshot.values else "missing"), snapshot


def drive(
    thread_id: str,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    *,
    cause: str,
    start_state: dict[str, Any] | None,
) -> str:
    """Run the thread until it parks or ends. Returns what it did: `started`, `resumed`, `continued`, `ended` (the
    thread had finished; nothing runs) or `missing` (no thread and no `start_state`). Always durability `sync`: each
    checkpoint is written before the next step (rule 5)."""
    config_: Any = {"configurable": {"thread_id": thread_id}}
    with thread_lock(thread_id):
        state, _ = thread_state(graph, thread_id)
        if state == "ended" or (state == "missing" and start_state is None):
            return state
        if state == "parked":
            graph.invoke(Command(resume={"cause": cause}), config_, durability="sync")
            return "resumed"
        if state == "crashed":
            graph.invoke(None, config_, durability="sync")
            return "continued"
        graph.invoke(start_state, config_, durability="sync")
        return "started"
