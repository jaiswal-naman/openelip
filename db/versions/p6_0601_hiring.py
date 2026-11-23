"""Hiring desk: organizations, roles, introductions, the shortlist and interest signals, with the guards the database
enforces alone (06-01).

The DDL is copied from docs/DATA_MODEL.md and lives beside this file in db/sql/, in the order it runs:

  p6_0601_types_functions.sql      3.1 the six hiring enums and the eval suite value role_intake, 3.2 add_business_days
  p6_0601_tables.sql               3.9 organizations, org_members, roles, role_requirements, introductions,
                                   shortlist_entries, interest_signals; 3.14 the foreign keys from earlier tables
  p6_0601_triggers_security.sql    4.7 the consent helpers and the three hiring guards, 5.3 the organization functions
                                   and role_preview, 5.4 policies, 5.7 grants

Three things differ from the document (each is a comment in the SQL and a line in DATA_MODEL): role_intake is a new eval
suite, interest_signals_check also requires an accepted introduction (owner decision G7), and role_preview carries the
time of the latest run (as_of). Two functions are new: my_org_invitations and introduction_role.

It runs in one transaction, so a failure leaves the database as it was. Additive on the live database: back it up first,
then apply it by hand. downgrade() exists for the CI migration check on a scratch database and is never run against a
database whose rows matter (its SQL refuses to run on a database called app).
"""

from pathlib import Path

from alembic import op

revision = "p6_0601_hiring"
down_revision = "p4_0401_match_brief"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
UPGRADE_FILES = ("p6_0601_types_functions.sql", "p6_0601_tables.sql", "p6_0601_triggers_security.sql")


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for name in UPGRADE_FILES:
        op.execute(_read(name))
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(_read("p6_0601_downgrade.sql"))
    op.execute("RESET ROLE")
