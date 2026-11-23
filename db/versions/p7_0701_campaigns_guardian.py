"""Campaigns and the Guardian: eight tables, the two-key and append-only guards, record_state_change (07-01).

The DDL is copied from docs/DATA_MODEL.md and lives beside this file in db/sql/, in the order it runs:

  p7_0701_types_tables.sql        the campaign and control enums, 3.10 (campaigns, campaign_steps, company_dossiers,
                                  agent_runs, agent_actions, approvals, autonomy_settings, guardian_checks), 3.14 the
                                  foreign keys of Phase 7 columns (brief_items, outbox, packets, applications,
                                  llm_calls)
  p7_0701_triggers_security.sql   4.5 and 4.6 (the two-key guard, approvals, the outbox guard), record_state_change,
                                  5.3 policies, 5.7 grants

Two things differ from the document (each is a comment in the SQL and a line in DATA_MODEL):
campaigns.follow_up_email is new, and undo_until is set once when a follow-up is scheduled instead of at insert.
record_state_change is new.

It runs in one transaction, so a failure leaves the database as it was. Additive on the live database: back it up first,
then apply it by hand. downgrade() exists for the CI migration check on a scratch database and is never run against a
database whose rows matter (its SQL refuses to run on a database called app).
"""

from pathlib import Path

from alembic import op

revision = "p7_0701_campaigns_guardian"
down_revision = "p56_merge_chat_hiring"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
UPGRADE_FILES = ("p7_0701_types_tables.sql", "p7_0701_triggers_security.sql")


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for name in UPGRADE_FILES:
        op.execute(_read(name))
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(_read("p7_0701_downgrade.sql"))
    op.execute("RESET ROLE")
