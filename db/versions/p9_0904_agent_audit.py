"""Two row-level-security additions the MCP server needs (plan 09-04; NET-01, NET-03, TRU-03).

  (1) audit_log.actor_insert: an organization token's ask_twin and get_twin_answer calls are about a candidate (the
      subject, so the candidate can read them in their audit log), but the policy of Phase 3 accepted only a subject
      equal
      to the signed-in person. It now also accepts, for a request made with an agent token, a subject who has an
      introduction with the row's organization. The check reads `introductions` under the token's own policy
      (agent_introductions), so it holds only for accepted introductions of the token's organization. Every other row is
      as before.
  (2) agent_feedback: a candidate's token with matches:read may read that candidate's own feedback_events rows, which is
      how search_matches tells a saved match from an unsaved one. Without it the answer would be "not saved" for every
      match, which is a guess.

The SQL runs as app_owner (the policies belong to it). The policy in (2) is guarded by an existence check.
downgrade() puts actor_insert back as plan 03-04 wrote it and drops agent_feedback.
"""

from alembic import op

revision = "p9_0904_agent_audit"
down_revision = "p9_0901_twin_agentnet"
branch_labels = None
depends_on = None

UPGRADE = """
ALTER POLICY actor_insert ON audit_log
  WITH CHECK (actor_user_id = app_user_id() AND agent_token_id IS NOT DISTINCT FROM app_agent_token_id()
              AND (subject_user_id IS NULL OR subject_user_id = app_user_id()
                   OR (app_agent_token_id() IS NOT NULL AND organization_id IS NOT NULL
                       AND EXISTS (SELECT 1 FROM introductions i
                                    WHERE i.user_id = subject_user_id AND i.organization_id =
                                    audit_log.organization_id))));

DO $pol$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'feedback_events'
                  AND policyname = 'agent_feedback') THEN
    CREATE POLICY agent_feedback ON feedback_events FOR SELECT TO app_api
      USING (user_id = app_user_id() AND app_agent_token_id() IS NOT NULL AND app_scope_ok('matches:read'));
  END IF;
END $pol$;
"""

DOWNGRADE = """
ALTER POLICY actor_insert ON audit_log
  WITH CHECK (actor_user_id = app_user_id() AND agent_token_id IS NOT DISTINCT FROM app_agent_token_id()
              AND (subject_user_id IS NULL OR subject_user_id = app_user_id()));
DROP POLICY IF EXISTS agent_feedback ON feedback_events;
"""


def upgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(UPGRADE)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE)
    op.execute("RESET ROLE")
