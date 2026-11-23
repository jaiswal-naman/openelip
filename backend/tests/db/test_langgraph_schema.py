"""Revision p7_0702_langgraph (plan 07-01): the checkpointer's schema exists only through Alembic, CI notices a change
in the library's own DDL, and a campaign's thread leaves with the campaign.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back.
"""

import hashlib
import importlib.util
from contextlib import contextmanager
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import make_world

REPO = Path(__file__).resolve().parents[3]
DATA_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
LIBRARY_TABLES = (*DATA_TABLES, "checkpoint_migrations")


@contextmanager
def refused(conn: Connection):
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def revision_module():
    path = REPO / "db" / "versions" / "p7_0702_langgraph.py"
    spec = importlib.util.spec_from_file_location("p7_0702_langgraph", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def act_as(conn: Connection, role: str) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))


def add_thread_rows(conn: Connection, thread_id: str) -> None:
    """One row of each data table for the thread, as the saver writes them (only the columns that have no default)."""
    conn.execute(
        text(
            """INSERT INTO langgraph.checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
               VALUES (:t, '', 'c1', '{}', '{}')"""
        ),
        {"t": thread_id},
    )
    conn.execute(
        text(
            """INSERT INTO langgraph.checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type)
               VALUES (:t, '', 'x', '1', 'empty')"""
        ),
        {"t": thread_id},
    )
    conn.execute(
        text(
            """INSERT INTO langgraph.checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel,
                                                        type, blob)
               VALUES (:t, '', 'c1', 'task', 0, 'x', 'empty', '\\x00')"""
        ),
        {"t": thread_id},
    )


def thread_rows(conn: Connection, thread_id: str) -> dict[str, int]:
    return {
        table: one(conn, f"SELECT count(*) FROM langgraph.{table} WHERE thread_id = :t", t=thread_id)
        for table in DATA_TABLES
    }


# ---- the schema and the library's migration list ---------------------------------------------------------------------


def test_the_schema_holds_the_four_library_tables(conn):
    tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'langgraph'")).scalars())
    assert tables == set(LIBRARY_TABLES)


def test_the_library_migrations_were_all_applied(conn):
    installed = PostgresSaver.MIGRATIONS
    assert one(conn, "SELECT max(v) FROM langgraph.checkpoint_migrations") == len(installed) - 1
    assert one(conn, "SELECT count(*) FROM langgraph.checkpoint_migrations") == len(installed)


def test_the_revision_recorded_the_installed_migration_list(conn):
    """If this fails the library's DDL changed under us: add a revision that applies the new migrations (the runtime
    roles have no CREATE in the schema, so setup() at run time would fail), then record the new count and hash."""
    revision = revision_module()
    installed = PostgresSaver.MIGRATIONS
    assert revision.LANGGRAPH_MIGRATION_COUNT == len(installed)
    assert revision.LANGGRAPH_MIGRATIONS_SHA256 == hashlib.sha256("\n".join(installed).encode()).hexdigest()


def test_the_tables_belong_to_the_owner_not_to_a_runtime_login(conn):
    owners = set(conn.execute(text("SELECT tableowner FROM pg_tables WHERE schemaname = 'langgraph'")).scalars())
    assert owners == {"app_owner"}
    assert one(conn, "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'langgraph'") == "app_owner"


# ---- who may reach it ------------------------------------------------------------------------------------------------


def test_the_worker_can_use_the_schema_and_write_every_data_table(conn):
    act_as(conn, "app_worker")
    add_thread_rows(conn, "campaign:11111111-1111-1111-1111-111111111111")
    for table in DATA_TABLES:
        assert one(conn, f"SELECT count(*) FROM langgraph.{table}") == 1
        conn.execute(text(f"DELETE FROM langgraph.{table}"))
    conn.execute(text("SELECT max(v) FROM langgraph.checkpoint_migrations"))


def test_no_runtime_login_may_create_anything_in_the_schema(conn):
    for role in ("app_api", "app_worker", "app_match"):
        assert not one(conn, "SELECT has_schema_privilege(:r, 'langgraph', 'CREATE')", r=role), role
    for role in ("app_api", "app_match"):
        assert not one(conn, "SELECT has_schema_privilege(:r, 'langgraph', 'USAGE')", r=role), role
    assert one(conn, "SELECT has_schema_privilege('app_worker', 'langgraph', 'USAGE')")
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        conn.execute(text("CREATE TABLE langgraph.sneaky (x int)"))
    assert "permission denied" in str(caught.value)


def test_the_api_login_cannot_read_a_checkpoint(conn):
    act_as(conn, "app_api")
    with refused(conn) as caught:
        conn.execute(text("SELECT count(*) FROM langgraph.checkpoints"))
    assert "permission denied" in str(caught.value)


def test_no_login_may_run_the_thread_deletion_function_directly(conn):
    for role in ("app_api", "app_worker", "app_match"):
        assert not one(conn, "SELECT has_function_privilege(:r, 'langgraph_delete_thread(text)', 'EXECUTE')", r=role), (
            role
        )


def test_row_level_security_is_off_in_the_schema(conn):
    assert not conn.execute(
        text(
            """SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'langgraph' AND c.relkind = 'r' AND c.relrowsecurity"""
        )
    ).all()


# ---- erasure ---------------------------------------------------------------------------------------------------------


def test_deleting_a_campaign_removes_its_thread_in_the_same_transaction_and_only_that_thread(conn):
    world = make_world(conn)
    other = make_world(conn)
    mine, theirs = f"campaign:{world.campaign}", f"campaign:{other.campaign}"
    add_thread_rows(conn, mine)
    add_thread_rows(conn, theirs)
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM campaigns WHERE id = :c"), {"c": world.campaign})
    assert thread_rows(conn, mine) == dict.fromkeys(DATA_TABLES, 0)
    assert thread_rows(conn, theirs) == dict.fromkeys(DATA_TABLES, 1)


def test_erasing_the_person_removes_their_campaign_threads(conn):
    world = make_world(conn)
    thread = f"campaign:{world.campaign}"
    add_thread_rows(conn, thread)
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": world.user})
    assert thread_rows(conn, thread) == dict.fromkeys(DATA_TABLES, 0)


def test_the_migrations_table_is_left_alone_by_a_deletion(conn):
    world = make_world(conn)
    before = one(conn, "SELECT count(*) FROM langgraph.checkpoint_migrations")
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM campaigns WHERE id = :c"), {"c": world.campaign})
    assert one(conn, "SELECT count(*) FROM langgraph.checkpoint_migrations") == before
