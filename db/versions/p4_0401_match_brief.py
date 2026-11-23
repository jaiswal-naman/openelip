"""Matches and briefs: the decision ledger, its evidence and feedback, the brief tables and the match role (04-01).

The DDL is copied from docs/DATA_MODEL.md and lives beside this file in db/sql/, in the order it runs:

  p4_0401_types_functions.sql      3.1 enums, 3.2 company_key(s), 4.1 compile_constraints, constraint_violations,
                                   unverified_fields
  p4_0401_tables.sql               3.6 match_runs, match_decisions, match_evidence, feedback_events;
                                   3.7 briefs, brief_items
  p4_0401_triggers_security.sql    4.2 citation and completeness triggers, 4.5 append-only, 5 policies and grants for
                                   app_api, app_worker and app_match, 7.1 the 90-day step of purge_expired

The login role app_match (DATA_MODEL 5.1) is made here, as 0002_platform makes app_api and app_worker, before the
objects are, because only the bootstrap login may create a role. It is never dropped: roles belong to the whole server.

It runs in one transaction, so a failure leaves the database as it was. Additive on the live database: back it up first,
then apply it by hand. downgrade() exists for the CI migration check on a scratch database and is never run against a
database whose rows matter (its SQL refuses to run on a database called app).
"""

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from psycopg import sql

revision = "p4_0401_match_brief"
down_revision = "p3_0303_skill_fks"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
# The development password of the match role. .env.example repeats it, and app.platform.config refuses it whenever
# APP_BASE_URL is not a localhost address.
MATCH_ROLE = "app_match"
PASSWORD_VARIABLE = "APP_DB_MATCH_PASSWORD"
DEV_PASSWORD = "app_match_local_only"
UPGRADE_FILES = ("p4_0401_types_functions.sql", "p4_0401_tables.sql", "p4_0401_triggers_security.sql")


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def _create_match_role() -> None:
    """DATA_MODEL 5.1. A role that already exists (roles are cluster-wide) is left as it is."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": MATCH_ROLE}).scalar():
        return
    password = os.environ.get(PASSWORD_VARIABLE) or DEV_PASSWORD
    # The raw psycopg connection joins the migration's transaction; sql.Literal quotes any password safely.
    statement = sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
        sql.Identifier(MATCH_ROLE), sql.Literal(password)
    )
    bind.connection.driver_connection.execute(statement)


def upgrade() -> None:
    _create_match_role()
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for name in UPGRADE_FILES:
        op.execute(_read(name))
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(_read("p4_0401_downgrade.sql"))
    op.execute("RESET ROLE")
