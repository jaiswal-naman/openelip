"""Trust: consents, audit_log, data_requests, and the functions that erase a person and expire an export (plan 03-01).

The DDL is copied from docs/DATA_MODEL.md: 2.1 types, 3.3 tables, 4.5 append-only triggers, 5.3 and 5.7 row-level
security and grants, 7.2 erase_user and purge_expired. purge_expired holds only the export-archive sweep for now; the
phases that add the other retained tables add their own steps.

audit_log is created here, guarded by to_regclass, because 0002_platform (DATA_MODEL 3.3 reduced to users, sessions and
login_codes) never made it. The five enums are guarded the same way. Foreign keys to tables of later phases
(consents.organization_id and role_id, audit_log.organization_id and agent_token_id) are plain nullable columns until
the phase that creates the target table adds the constraint (DATA_MODEL 3.14).

Additive only. The downgrade drops what this revision made, so it restores the state of the revision below it.

Phase 2 (the job index) and Phase 3 were built side by side, both starting at 0002_platform. They are joined in one
straight line, with this revision on top of p2_0201_job_index, and not by an Alembic merge revision: with two branches
Alembic does not fix the order in which it downgrades them, and the job index's downgrade can drop its shared types
only after the record tables that use them are gone. In a line the order is always the same.
"""

import sqlalchemy as sa
from alembic import op

revision = "p3_0301_trust"
down_revision = "p2_0201_job_index"
branch_labels = None
depends_on = None

# DATA_MODEL 2.1. Each is created only if no earlier revision made it.
TYPES_SQL = {
    "consent_purpose": """CREATE TYPE consent_purpose AS ENUM ('terms', 'ai_processing', 'brief_email',
                                       'open_to_introductions', 'introduction', 'agent_access')""",
    "consent_action": "CREATE TYPE consent_action AS ENUM ('granted', 'withdrawn')",
    "actor_type": """CREATE TYPE actor_type AS ENUM ('system', 'candidate', 'employer', 'agent', 'outside_agent',
                                  'operator')""",
    "data_request_kind": "CREATE TYPE data_request_kind AS ENUM ('export', 'deletion')",
    "data_request_status": "CREATE TYPE data_request_status AS ENUM ('open', 'in_progress', 'completed', 'failed')",
}

# DATA_MODEL 3.3, 4.5 (the append-only trigger) and 5.3 and 5.7 (the policies and the grants), for consents and
# data_requests. forbid_mutation() comes from 0002_platform.
CONSENTS_AND_REQUESTS_SQL = """
-- Grants and withdrawals, append-only. A grant is active while no withdrawal row points at it.
CREATE TABLE consents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose         consent_purpose NOT NULL,
  action          consent_action  NOT NULL,
  withdraws_id    uuid,                         -- withdrawal rows: the grant they end
  organization_id uuid,                         -- purpose 'introduction': the employer
  role_id         uuid,                         -- purpose 'introduction': the role
  text_version    text            NOT NULL,     -- version of the wording the person saw
  created_at      timestamptz     NOT NULL DEFAULT now(),
  UNIQUE (id, user_id, purpose),
  -- A withdrawal can only end the same person's grant for the same purpose.
  FOREIGN KEY (withdraws_id, user_id, purpose) REFERENCES consents (id, user_id, purpose) ON DELETE CASCADE,
  CHECK ((action = 'withdrawn') = (withdraws_id IS NOT NULL)),
  CHECK (action = 'withdrawn'
         OR (purpose = 'introduction') = (organization_id IS NOT NULL AND role_id IS NOT NULL))
);
CREATE UNIQUE INDEX consents_one_withdrawal ON consents (withdraws_id) WHERE withdraws_id IS NOT NULL;
CREATE INDEX consents_lookup_idx ON consents (user_id, purpose, organization_id, role_id) WHERE action = 'granted';
CREATE TRIGGER consents_append_only BEFORE UPDATE OR DELETE ON consents
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE data_requests (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid                REFERENCES users(id) ON DELETE SET NULL,
  subject_user_id    uuid                NOT NULL,   -- no foreign key: kept after erasure so a restore can be re-erased
  kind               data_request_kind   NOT NULL,
  status             data_request_status NOT NULL DEFAULT 'open',
  due_at             timestamptz         NOT NULL,
  export_archive     bytea,                          -- export: the archive as a Fernet token; NULL after 7 days
  export_expires_at  timestamptz,
  store_results      jsonb               NOT NULL DEFAULT '{}',  -- per store: what was exported or removed
  created_at         timestamptz         NOT NULL DEFAULT now(),
  completed_at       timestamptz,
  CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX data_requests_one_open_deletion
  ON data_requests (subject_user_id) WHERE kind = 'deletion' AND status IN ('open', 'in_progress');

ALTER TABLE consents ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON consents TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON consents TO app_worker USING (true) WITH CHECK (true);

-- DATA_MODEL 5.3 checks only user_id here. erase_user trusts a deletion request's subject, so the API role may also
-- write only requests whose subject is the person it acts for: it cannot open a request that erases someone else.
ALTER TABLE data_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON data_requests TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND subject_user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON data_requests TO app_worker USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON consents TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON data_requests TO app_api, app_worker;
"""

# DATA_MODEL 3.3, 4.5, 5.3 and 5.7 for audit_log; run only when the table does not exist yet.
AUDIT_LOG_SQL = """
-- Every automated decision and every privileged or outside access, append-only (TRU-03, NET-03).
CREATE TABLE audit_log (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at      timestamptz NOT NULL DEFAULT now(),
  event_type      text        NOT NULL CHECK (event_type ~ '^[a-z_]+\\.[a-z_]+$'),  -- 'match.run_completed'
  actor_type      actor_type  NOT NULL,
  actor_user_id   uuid        REFERENCES users(id) ON DELETE SET NULL,
  subject_user_id uuid        REFERENCES users(id) ON DELETE CASCADE,   -- the person the event is about
  organization_id uuid,
  agent_token_id  uuid,                    -- set when an outside agent made the call
  entity_table    text,
  entity_id       text,
  details         jsonb       NOT NULL DEFAULT '{}'   -- ids, counts and codes; never resume, chat or contact text
);
CREATE INDEX audit_log_subject_idx ON audit_log (subject_user_id, created_at DESC);
CREATE INDEX audit_log_event_idx   ON audit_log (event_type, created_at DESC);
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY subject_read ON audit_log FOR SELECT TO app_api
  USING (subject_user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY actor_insert ON audit_log FOR INSERT TO app_api      -- NET-03: refusals are logged against the token
  WITH CHECK (actor_user_id = app_user_id() AND agent_token_id IS NOT DISTINCT FROM app_agent_token_id());
CREATE POLICY worker_all ON audit_log TO app_worker USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON audit_log TO app_api, app_worker;
"""

# DATA_MODEL 7.2. Erasure is one transaction: the person's rows go by cascade; rows that must outlive them (the cost
# ledger, an audit actor) lose the reference instead. app.maintenance lets the append-only triggers through for that
# transaction only.
FUNCTIONS_SQL = """
CREATE FUNCTION erase_user(p_user_id uuid, p_request_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_email text;
  n_codes integer;
BEGIN
  PERFORM 1 FROM data_requests r
  WHERE r.id = p_request_id AND r.kind = 'deletion' AND r.subject_user_id = p_user_id
    AND r.status IN ('open', 'in_progress');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no open deletion request % for this user', p_request_id USING ERRCODE = 'check_violation';
  END IF;
  PERFORM set_config('app.maintenance', 'on', true);       -- this transaction only

  SELECT email INTO v_email FROM users WHERE id = p_user_id;
  DELETE FROM login_codes WHERE lower(email) = lower(v_email);
  GET DIAGNOSTICS n_codes = ROW_COUNT;
  DELETE FROM users WHERE id = p_user_id;                  -- every user_id foreign key cascades or sets NULL

  INSERT INTO audit_log (event_type, actor_type, entity_table, entity_id, details)
  VALUES ('user.erased', 'system', 'data_requests', p_request_id::text, '{}');
  UPDATE data_requests
     SET status = 'completed', completed_at = now(),
         store_results = store_results || jsonb_build_object('postgres', 'deleted', 'login_codes', n_codes)
   WHERE id = p_request_id;
  RETURN jsonb_build_object('postgres', 'deleted');
END;
$$;

-- The retention sweep (DATA_MODEL 7.1), run daily by the worker. For now it only clears export archives that have
-- expired: the person had 7 days to download it.
CREATE FUNCTION purge_expired() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  n integer;
BEGIN
  UPDATE data_requests SET export_archive = NULL
   WHERE export_archive IS NOT NULL AND export_expires_at < now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN jsonb_build_object('export_links', n);
END;
$$;

REVOKE EXECUTE ON FUNCTION erase_user(uuid, uuid), purge_expired() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION erase_user(uuid, uuid), purge_expired() TO app_worker;
"""

DOWNGRADE_SQL = """
DROP FUNCTION erase_user(uuid, uuid), purge_expired();
DROP TABLE data_requests, audit_log, consents;
DROP TYPE data_request_status, data_request_kind, actor_type, consent_action, consent_purpose;
"""


def _exists(lookup: str, name: str) -> bool:
    """Whether the database already has the object: lookup is the catalog function to_regtype or to_regclass."""
    return bool(op.get_bind().execute(sa.text(f"SELECT {lookup}('{name}') IS NOT NULL")).scalar())


def upgrade() -> None:
    # Everything is created, and every grant issued, by app_owner (ARCHITECTURE 7.1), as in 0002_platform.
    op.execute("SET ROLE app_owner")
    for name, create in TYPES_SQL.items():
        if not _exists("to_regtype", name):
            op.execute(create)
    op.execute(CONSENTS_AND_REQUESTS_SQL)
    if not _exists("to_regclass", "public.audit_log"):
        op.execute(AUDIT_LOG_SQL)
    op.execute(FUNCTIONS_SQL)
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
