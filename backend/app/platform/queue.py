"""The Postgres queue (plan 01-02 implements it; ARCHITECTURE 5.1 and 5.7)."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

Handler = Callable[[Connection, dict[str, Any]], None]
ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


def enqueue(
    conn: Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    unique_key: str,
    queue: str = "default",
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Add a job in the caller's transaction and return its id, or None when the unique key already exists.

    The id is an int because queue_jobs.id is a bigint (DATA_MODEL 3.13), not a UUID as the plans first wrote.
    """
    raise NotImplementedError("plan 01-02")


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Decorator that registers fn(conn, payload) as the handler for jobs of this kind."""
    raise NotImplementedError("plan 01-02")


def run_once(conn_factory: ConnectionFactory, worker_id: str) -> int:
    """Claim and run due jobs; returns how many were processed."""
    raise NotImplementedError("plan 01-02")
