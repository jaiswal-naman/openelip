"""The LangGraph checkpointer schema and the campaign thread deletion trigger (07-01).

Three steps, as DATA_MODEL 3.15 says:

  1. p7_0702_schema.sql        creates schema langgraph and its grants, run as app_owner on an autocommit connection so the
                               schema is committed before the library uses it
  2. PostgresSaver.setup()     the library creates and owns checkpoints, checkpoint_blobs, checkpoint_writes and
                               checkpoint_migrations (three of its indexes are built CONCURRENTLY, which cannot run inside a
                               transaction), on a separate autocommit connection as app_owner with search_path langgraph
  3. p7_0702_delete_thread.sql the deletion function and the trigger that removes a campaign's thread with the campaign

LANGGRAPH_MIGRATION_COUNT and LANGGRAPH_MIGRATIONS_SHA256 record the library's migration list as this revision knew it
(PostgresSaver.MIGRATIONS, joined by newlines, hashed). tests/db/test_langgraph_schema.py fails when the installed list
differs, so an upgrade of the library that changes its DDL is noticed in CI and handled by a new revision, never by
setup() at run time: the runtime roles have no CREATE in the schema.

Additive on the live database: back it up first, then apply it by hand as app_owner. downgrade() exists for the CI
migration check on a scratch database and is never run against a database whose rows matter.
"""

from pathlib import Path

import psycopg
from alembic import op
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

revision = "p7_0702_langgraph"
down_revision = "p7_0701_campaigns_guardian"
branch_labels = None
depends_on = None

LANGGRAPH_MIGRATION_COUNT = 10
LANGGRAPH_MIGRATIONS_SHA256 = "b61d83ce19b67141d851d7fa29a73ecc6f5cd8e9d45c1aa0f6b6f9ebe50f26bf"

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def upgrade() -> None:
    bind = op.get_bind()
    url = bind.engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with op.get_context().autocommit_block():
        op.execute("SET ROLE app_owner")
        op.execute(_read("p7_0702_schema.sql"))
        op.execute("RESET ROLE")
        # A separate connection: Alembic's own never changes role, search path or row factory.
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as saver_connection:
            saver_connection.execute("SET ROLE app_owner")
            saver_connection.execute("SET search_path = langgraph")
            PostgresSaver(saver_connection).setup()  # type: ignore[arg-type]
    op.execute("SET ROLE app_owner")
    op.execute(_read("p7_0702_delete_thread.sql"))
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(_read("p7_0702_downgrade.sql"))
    op.execute("RESET ROLE")
