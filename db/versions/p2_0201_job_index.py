"""Job index: the typed shape of DATA_MODEL 3.5, the requirement table and the guards that keep its spans true.

Converts job_sources, jobs and fetch_runs in place (DATA_MODEL 8, step 2) without losing a row, then adds skills and
job_requirements with their triggers and grants. The SQL lives beside this file in db/sql/, in the order it runs:

  p2_0201_types_convert.sql          enum types, then the in-place conversion, split at the backfill marker
  (the backfill, run here)           what ingest already stored becomes the typed columns
  p2_0201_tables_triggers_grants.sql skills, job_requirements, span and version guards, append-only triggers, grants

It runs in one transaction as app_owner, with a lock timeout, so a failure leaves the database as it was. Forward only
on the live database: back it up and rehearse first (db/rehearse_upgrade.py). downgrade() exists for the CI migration
check on a scratch database and is never run against a database whose rows matter.
"""

import os
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "p2_0201_job_index"
down_revision = "0002_platform"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
BACKFILL_MARKER = "-- @@BACKFILL@@"
# Assumption: a migration that cannot get its table locks within this time stops and rolls back instead of queueing
# behind a long query and blocking every reader of the job index. Run it again when the API is quiet.
LOCK_TIMEOUT = "15s"
# The model behind any embeddings already stored (AI_SYSTEM 3.1). A migration cannot import the application, so the
# default is repeated here; EMBEDDING_MODEL overrides it, as it does for the application.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

# DATA_MODEL 8. is_remote false meant "the posting is onsite or hybrid"; null meant "does not say". The old free-text
# employment_type is kept as employment_type_raw and mapped where it plainly matches. materially_changed_at starts at
# first_seen_at. A closed row that never recorded when gets its last verified time.
BACKFILL_SQL = """
UPDATE jobs SET
  work_arrangements = CASE is_remote WHEN true THEN '{remote}'::work_arrangement[]
                                     WHEN false THEN '{onsite,hybrid}'::work_arrangement[]
                                     ELSE '{}'::work_arrangement[] END,
  employment_type = CASE
    WHEN employment_type_raw ILIKE 'full%'     THEN 'full_time'
    WHEN employment_type_raw ILIKE 'part%'     THEN 'part_time'
    WHEN employment_type_raw ILIKE 'contract%' THEN 'contract'
    WHEN employment_type_raw ILIKE 'intern%'   THEN 'internship'
    WHEN employment_type_raw ILIKE 'temp%'     THEN 'temporary'
    ELSE 'unknown' END::employment_type,
  materially_changed_at = first_seen_at,
  closed_at = CASE WHEN status = 'closed' THEN coalesce(closed_at, last_verified_at) END,
  embedding_model = CASE WHEN embedding IS NOT NULL THEN CAST(:embedding_model AS text) END
"""


def _read(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def upgrade() -> None:
    before_backfill, after_backfill = _read("p2_0201_types_convert.sql").split(BACKFILL_MARKER)
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    op.execute(before_backfill)
    # Its own statement: psycopg cannot bind a parameter inside a multi-statement string.
    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    op.get_bind().execute(sa.text(BACKFILL_SQL), {"embedding_model": model})
    op.execute(after_backfill)
    op.execute(_read("p2_0201_tables_triggers_grants.sql"))
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("SET ROLE app_owner")
    op.execute(_read("p2_0201_downgrade.sql"))
    op.execute("RESET ROLE")
    op.execute("RESET lock_timeout")
