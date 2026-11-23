"""The two lossy downgrades of Phase 3 refuse to run on the live database.

p3_0302_record drops the five record tables and p3_0301_trust drops the consents, the audit log and the data requests.
db/env.py connects to the database called app when DATABASE_URL_MIGRATE is not set, so `alembic downgrade` run by hand
from a checkout would erase every career record there once Phase 3 is upgraded. The job index's downgrade has the same
guard as its first statement (test_job_index_schema.py); the CI migration check and every test use other names.
"""

from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

REPO = Path(__file__).resolve().parents[3]
REVISIONS = ["p3_0301_trust", "p3_0302_record"]
TABLES = {"p3_0301_trust": "consents", "p3_0302_record": "profile_facts"}


def downgrade_sql(revision: str) -> str:
    """The SQL the revision's downgrade() runs, as the revision file defines it."""
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config).get_revision(revision)
    assert script is not None
    return str(script.module.DOWNGRADE_SQL)


def guard_of(sql: str) -> str:
    """The first statement: a DO block that ends at the first END $$;."""
    return sql.strip().split("END $$;", 1)[0] + "END $$;"


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def live_database_is_called_app(conn: Connection) -> None:
    """Make current_database() answer app for this transaction. A database of that name must not be made here (the real
    one may be on this server), so a function of that name comes first on the search path instead."""
    conn.execute(text("CREATE SCHEMA live_stand_in"))
    conn.execute(
        text("CREATE FUNCTION live_stand_in.current_database() RETURNS name LANGUAGE sql AS $$ SELECT 'app'::name $$")
    )
    conn.execute(text("SET LOCAL search_path = live_stand_in, pg_catalog, public"))


@pytest.mark.parametrize("revision", REVISIONS)
def test_the_downgrade_refuses_to_run_on_the_live_database_before_it_changes_anything(conn, revision):
    sql = downgrade_sql(revision)
    assert sql.strip().startswith("DO $$"), "the guard must come before every DROP"
    live_database_is_called_app(conn)

    with refused(conn) as caught:
        conn.execute(text(sql))  # the whole downgrade: it must stop at its first statement

    assert caught.value.orig.diag.message_primary == "refusing a lossy downgrade on app"
    assert conn.execute(text("SELECT to_regclass(:t)"), {"t": TABLES[revision]}).scalar_one() is not None


@pytest.mark.parametrize("revision", REVISIONS)
def test_the_guard_lets_any_other_database_through(conn, revision):
    """The migration check in CI and every test run the downgrade on databases with other names."""
    conn.execute(text(guard_of(downgrade_sql(revision))))  # raises nothing
    assert conn.execute(text("SELECT to_regclass(:t)"), {"t": TABLES[revision]}).scalar_one() is not None
