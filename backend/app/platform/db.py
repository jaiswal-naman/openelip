"""Database access. Only this module creates engines (ARCHITECTURE 3.3, rule 3).

One SQLAlchemy 2 engine per database role, on psycopg 3. transaction() runs a unit of work in one transaction and sets
app.user_id for that transaction only, so row-level security sees the signed-in person and a pooled connection cannot
carry the setting into the next request (ARCHITECTURE 7.2).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from . import config
from .config import Role

_engines: dict[Role, Engine] = {}


def engine(role: Role) -> Engine:
    if role not in _engines:
        url = make_url(config.get().database_url(role)).set(drivername="postgresql+psycopg")
        _engines[role] = create_engine(url, pool_pre_ping=True)
    return _engines[role]


@contextmanager
def transaction(user_id: UUID | None = None, role: Role = "api") -> Iterator[Connection]:
    """One transaction that commits when the block ends and rolls back if it raises.

    A callee that commits itself (the queue and the relay do) ends the transaction, and the user id with it; only the
    worker role does that, and it needs no user id.
    """
    with engine(role).connect() as connection:
        connection.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id or "")})
        yield connection
        connection.commit()


def connect(role: Role = "worker") -> "psycopg.Connection[Any]":
    """A plain psycopg connection, for ingest code that has not moved to SQLAlchemy text() yet (plan 01-02)."""
    return psycopg.connect(config.get().database_url(role))
