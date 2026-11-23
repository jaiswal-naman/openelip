"""Chat, tailoring and applications: the seven tables of Phase 5 and the guards the database enforces alone (05-01).

The DDL is copied from docs/DATA_MODEL.md and lives beside this file in db/sql/, in the order it runs:

  p5_0501_types_tables.sql        2.1 the eight enums, 3.8 tailored_resumes, resume_edits, application_packets,
                                  applications, application_events, 3.12 chat_sessions, chat_messages, 3.14 the
                                  chat_messages -> llm_calls key
  p5_0501_triggers_security.sql   4.3 the invariant-3 trigger and the decide-once trigger, 4.5 the append-only status
                                  history, 5.3 and 5.7 row-level security and grants for app_api and app_worker

Three things differ from DATA_MODEL and are recorded there (Phase 5 deviations): chat_messages.finish_reason, the
partial unique index application_packets_one_current_job, and resume_edits_decide_once(), which replaces
write_once_columns('decision', 'decided_at') because that function would refuse the first decision.

It runs in one transaction, so a failure leaves the database as it was. Additive on the live database: back it up first,
then apply it by hand. downgrade() exists for the CI migration check on a scratch database and is never run against a
database whose rows matter (its SQL refuses to run on a database called app).
"""

from pathlib import Path

from alembic import op

revision = "p5_0501_chat_tailor_apply"
down_revision = "p4_0401_match_brief"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
UPGRADE_FILES = ("p5_0501_types_tables.sql", "p5_0501_triggers_security.sql")


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for name in UPGRADE_FILES:
        op.execute(_read(name))
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(_read("p5_0501_downgrade.sql"))
    op.execute("RESET ROLE")
