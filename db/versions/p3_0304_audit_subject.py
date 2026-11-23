"""audit_log: the API role writes only rows about the person it acts for, or about no one (Phase 3 verification, W4).

DATA_MODEL 5.3 (and p3_0301_trust, which copied it) lets the API role insert an audit row when its actor is the
signed-in person and its agent token matches, and says nothing about the subject. A request acting for A could
therefore put a row in B's audit log, which TRU-03 shows to B. The policy now also needs the subject to be the caller
or empty.

A row about someone other than the actor is what an organization token will write once those exist (NET-03 needs actor
and subject to differ); the phase that adds them relaxes this for that case.

This is a new revision and not an edit of p3_0301_trust because a database that is already at p3_0303_skill_fks (every
test database that has run the suite) would not pick up an edit of an earlier revision. It loses no data in either
direction, so its downgrade has no guard.
"""

from alembic import op

revision = "p3_0304_audit_subject"
down_revision = "p3_0303_skill_fks"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
ALTER POLICY actor_insert ON audit_log
  WITH CHECK (actor_user_id = app_user_id() AND agent_token_id IS NOT DISTINCT FROM app_agent_token_id()
              AND (subject_user_id IS NULL OR subject_user_id = app_user_id()));
"""

DOWNGRADE_SQL = """
ALTER POLICY actor_insert ON audit_log
  WITH CHECK (actor_user_id = app_user_id() AND agent_token_id IS NOT DISTINCT FROM app_agent_token_id());
"""


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # the table and its policies belong to app_owner
    op.execute(UPGRADE_SQL)
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
