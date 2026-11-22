"""Platform: roles, types, helpers and every Phase 1 table; adopts the three job tables unchanged.

The DDL is copied from docs/DATA_MODEL.md and reduced to what Phase 1 uses:
  section 3.1 types and 5.1 roles, 3.2 helpers, 3.3 users/sessions/login_codes, 3.7 outbox,
  3.13 model_registry/eval_runs/llm_calls/queue_jobs/idempotency_keys/eval_items, 4.5 triggers, 5.2 to 5.7 row-level
  security and grants.

Deliberately not here (INDEX "Deviations"): the in-place conversion of jobs, job_sources and fetch_runs to enums
(DATA_MODEL 8 step 2, Phase 2). Their rows are not touched: only their owner and the grants on them change.
Foreign keys to tables of later phases (llm_calls.organization_id, campaign_id, agent_run_id and outbox.agent_action_id)
are plain nullable columns; the phase that creates the target table adds the constraint.

Everything runs in one transaction, so a failure leaves the database as it was. The roles are cluster-wide and are
never dropped by the downgrade, because other databases on the same server use them.
"""

import os

import sqlalchemy as sa
from alembic import op
from psycopg import sql

revision = "0002_platform"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# Development passwords for the two login roles. .env.example repeats them, and app.platform.config refuses
# them whenever APP_BASE_URL is not a localhost address.
DEV_PASSWORDS = {
    "app_api": ("APP_DB_API_PASSWORD", "app_api_local_only"),
    "app_worker": ("APP_DB_WORKER_PASSWORD", "app_worker_local_only"),
}
JOB_TABLES = ("job_sources", "jobs", "fetch_runs")

TYPES_SQL = """
CREATE TYPE user_status     AS ENUM ('active', 'suspended', 'deleting');
CREATE TYPE brief_frequency AS ENUM ('daily', 'weekly', 'paused');
CREATE TYPE outbox_status   AS ENUM ('pending', 'sending', 'sent', 'failed', 'cancelled', 'unknown');
CREATE TYPE model_kind      AS ENUM ('chat', 'embedding');
CREATE TYPE llm_outcome     AS ENUM ('ok', 'schema_invalid', 'timeout', 'provider_error', 'refused');
CREATE TYPE queue_status    AS ENUM ('queued', 'running', 'succeeded', 'dead');
-- harness_smoke is the eval harness's own suite: it proves the runner and the gate, never product quality.
CREATE TYPE eval_suite      AS ENUM ('resume_extraction', 'requirement_extraction', 'evidence_faithfulness',
                                     'tailoring_integrity', 'matching_benchmark', 'injection',
                                     'constraint_property', 'wind_tunnel', 'record_proposals', 'chat',
                                     'dossier', 'twin', 'guardian', 'agent_drafts', 'harness_smoke');
CREATE TYPE eval_status     AS ENUM ('running', 'passed', 'failed', 'error');
"""

# Functions that need no table (DATA_MODEL 3.2 and 5.2).
HELPERS_SQL = """
-- Append-only guard. Rows leave these tables only through erase_user and purge_expired,
-- which set app.maintenance for their own transaction.
CREATE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

-- The API sets these with set_config(name, value, true) at the start of every transaction (ARCHITECTURE 7.2).
CREATE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app_agent_token_id() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.agent_token_id', true), '')::uuid $$;
"""

# DATA_MODEL 3.3, 3.7 and 3.13, in foreign-key order.
TABLES_SQL = """
CREATE TABLE users (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email                  text            NOT NULL CHECK (position('@' IN email) > 1),
  display_name           text,
  status                 user_status     NOT NULL DEFAULT 'active',
  is_synthetic           boolean         NOT NULL DEFAULT false,  -- demo or wind-tunnel account, never a real person
  timezone               text            NOT NULL DEFAULT 'UTC',  -- IANA name, validated by the application
  brief_frequency        brief_frequency NOT NULL DEFAULT 'daily',
  brief_weekday          smallint        CHECK (brief_weekday BETWEEN 1 AND 7),  -- ISO weekday, weekly briefs only
  undo_window_minutes    smallint        NOT NULL DEFAULT 10 CHECK (undo_window_minutes BETWEEN 1 AND 60),
  -- Career Record header, written only by the profile module
  record_version         bigint          NOT NULL DEFAULT 0,      -- +1 on every applied record change
  record_confirmed_at    timestamptz,                             -- first confirmation; NULL = nothing to match yet
  record_embedding       vector(384),                             -- built from confirmed facts only
  record_embedding_model text,
  last_active_at         timestamptz,
  created_at             timestamptz     NOT NULL DEFAULT now(),
  updated_at             timestamptz     NOT NULL DEFAULT now(),
  CHECK ((brief_frequency = 'weekly') = (brief_weekday IS NOT NULL)),
  CHECK ((record_embedding IS NULL) = (record_embedding_model IS NULL))
);
CREATE UNIQUE INDEX users_email_key ON users (lower(email));
-- Nightly selection: candidates who can receive a brief, by time zone.
CREATE INDEX users_due_idx ON users (timezone)
  WHERE status = 'active' AND brief_frequency <> 'paused' AND record_confirmed_at IS NOT NULL;
CREATE TRIGGER users_touch BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- A signed-in browser. The session token (sent as a cookie) is never stored, only its SHA-256.
CREATE TABLE sessions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_sha256 bytea       NOT NULL UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  last_seen_at timestamptz,
  revoked_at   timestamptz,                -- sign-out
  CHECK (expires_at > created_at)
);
CREATE INDEX sessions_user_idx ON sessions (user_id) WHERE revoked_at IS NULL;

-- One emailed sign-in code (PLT-02). Not tied to a user: the address may not have an account yet.
CREATE TABLE login_codes (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email       text        NOT NULL,
  code_hmac   bytea       NOT NULL,   -- HMAC-SHA256 keyed by APP_LOOKUP_KEY
  attempts    smallint    NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),  -- assumption: 5 tries per code
  expires_at  timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_codes_email_idx ON login_codes (lower(email), created_at DESC);

-- Models the gateway may call, with prices for the cost ledger and retention notes for /trust (TRU-04).
-- Which model serves which task is configuration (PLT-05), not a table.
CREATE TABLE model_registry (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  provider              text        NOT NULL,     -- 'anthropic', a second provider, 'local'
  model_name            text        NOT NULL,
  kind                  model_kind  NOT NULL,
  is_local              boolean     NOT NULL DEFAULT false,
  input_microusd_per_mtok  bigint,                -- micro-dollars per million tokens; NULL for local models
  output_microusd_per_mtok bigint,
  embedding_dimensions  integer,
  retention_note        text        NOT NULL,
  is_active             boolean     NOT NULL DEFAULT true,
  price_checked_at      timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, model_name),
  CHECK ((kind = 'embedding') = (embedding_dimensions IS NOT NULL)),
  CHECK (is_local OR (input_microusd_per_mtok IS NOT NULL AND output_microusd_per_mtok IS NOT NULL
                      AND price_checked_at IS NOT NULL))
);

CREATE TABLE eval_runs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suite          eval_suite  NOT NULL,
  git_sha        text        NOT NULL,
  config         jsonb       NOT NULL DEFAULT '{}',   -- models and prompt versions under test
  status         eval_status NOT NULL DEFAULT 'running',
  metrics        jsonb,                               -- set once, when the run finishes
  is_gate_passed boolean,
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  CHECK ((status = 'running') = (finished_at IS NULL)),
  CHECK (status = 'running' OR metrics IS NOT NULL)
);
CREATE INDEX eval_runs_suite_idx ON eval_runs (suite, started_at DESC);

-- The cost ledger: one row per model call, ids and numbers only (PLT-04). Append-only.
-- organization_id, campaign_id and agent_run_id get their foreign keys in the phases that create those tables.
CREATE TABLE llm_calls (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at         timestamptz NOT NULL DEFAULT now(),
  task               text        NOT NULL,      -- 'match.evidence', 'chat.turn', 'ingest.requirements' ...
  prompt_version     text        NOT NULL,
  model_id           uuid        NOT NULL REFERENCES model_registry(id),
  outcome            llm_outcome NOT NULL,
  user_id            uuid        REFERENCES users(id) ON DELETE SET NULL,
  organization_id    uuid,
  campaign_id        uuid,
  agent_run_id       uuid,
  eval_run_id        uuid        REFERENCES eval_runs(id) ON DELETE SET NULL,
  input_tokens       integer     NOT NULL DEFAULT 0,
  output_tokens      integer     NOT NULL DEFAULT 0,
  cache_read_tokens  integer     NOT NULL DEFAULT 0,
  cache_write_tokens integer     NOT NULL DEFAULT 0,
  cost_microusd      bigint      NOT NULL DEFAULT 0 CHECK (cost_microusd >= 0),
  latency_ms         integer     NOT NULL CHECK (latency_ms >= 0),
  attempt            smallint    NOT NULL DEFAULT 1
  -- no prompt or response text, by design (PLT-07)
);
CREATE INDEX llm_calls_campaign_idx ON llm_calls (campaign_id, created_at) WHERE campaign_id IS NOT NULL;
CREATE INDEX llm_calls_user_idx     ON llm_calls (user_id, created_at) WHERE user_id IS NOT NULL;
CREATE INDEX llm_calls_task_idx     ON llm_calls (task, created_at);

-- The Postgres-backed queue (PLT-06).
CREATE TABLE queue_jobs (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue           text         NOT NULL,
  kind            text         NOT NULL,                 -- 'match.nightly', 'ingest.fetch', 'campaign.resume' ...
  payload         jsonb        NOT NULL DEFAULT '{}',    -- ids only
  user_id         uuid         REFERENCES users(id) ON DELETE CASCADE,
  unique_key      text         NOT NULL UNIQUE,          -- 'match.nightly' plus the user and the local date
  status          queue_status NOT NULL DEFAULT 'queued',
  priority        smallint     NOT NULL DEFAULT 100,
  run_at          timestamptz  NOT NULL DEFAULT now(),   -- scheduled jobs: not before this time
  attempts        smallint     NOT NULL DEFAULT 0,
  max_attempts    smallint     NOT NULL DEFAULT 5,
  locked_by       text,
  locked_at       timestamptz,
  lease_seconds   integer      NOT NULL DEFAULT 300,
  last_error_code text,
  created_at      timestamptz  NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  CHECK (attempts <= max_attempts)
);
CREATE INDEX queue_jobs_claim_idx ON queue_jobs (queue, priority, run_at) WHERE status = 'queued';
CREATE INDEX queue_jobs_lease_idx ON queue_jobs (locked_at) WHERE status = 'running';

-- PLT-03: the first response to a write is replayed for a repeated key.
CREATE TABLE idempotency_keys (
  user_id         uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key             text        NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
  method          text        NOT NULL,
  path            text        NOT NULL,
  request_sha256  bytea       NOT NULL,
  response_status smallint,               -- NULL while the first request is in flight
  response_body   jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

CREATE TABLE eval_items (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  eval_run_id    uuid        NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  item_key       text        NOT NULL,      -- stable key of the gold-set item
  slice          text,                      -- 'constraint' plus the kind, 'persona' plus the name ...
  source_user_id uuid        REFERENCES users(id) ON DELETE CASCADE,  -- only if a consenting person's data is the item
  expected       jsonb,
  actual         jsonb,
  is_passed      boolean     NOT NULL,
  score          real,
  UNIQUE (eval_run_id, item_key)
);

-- Transactional outbox for every email (briefs, notices, approved outgoing messages).
-- agent_action_id gets its foreign key in the phase that creates agent_actions.
CREATE TABLE outbox (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dedupe_key          text          NOT NULL,   -- one outbox row per logical message
  user_id             uuid          REFERENCES users(id) ON DELETE CASCADE,
  template            text          NOT NULL,   -- 'signin', 'brief_v1', 'follow_up_v1' ...
  payload             jsonb         NOT NULL DEFAULT '{}',  -- ids only; recipient and body are resolved at send time
  agent_action_id     bigint,                   -- set when this message is an agent's outside action
  status              outbox_status NOT NULL DEFAULT 'pending',
  attempts            smallint      NOT NULL DEFAULT 0,
  not_before          timestamptz   NOT NULL DEFAULT now(),   -- the undo window ends here (GRD-04)
  expires_at          timestamptz,                            -- a brief is not sent after local noon
  locked_at           timestamptz,
  provider_message_id text,
  last_error_code     text,
  created_at          timestamptz   NOT NULL DEFAULT now(),
  sent_at             timestamptz,
  CONSTRAINT outbox_dedupe UNIQUE (dedupe_key),
  CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);
CREATE UNIQUE INDEX outbox_one_per_action ON outbox (agent_action_id) WHERE agent_action_id IS NOT NULL;
CREATE INDEX outbox_pending_idx ON outbox (not_before) WHERE status = 'pending';
"""

# Functions that read the tables (a SQL function body is checked when it is created), and the triggers (DATA_MODEL 4.5).
AFTER_TABLES_SQL = """
-- Invariant 8: an eval run's numbers are frozen once it finishes.
CREATE FUNCTION eval_runs_freeze() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' OR OLD.status <> 'running' THEN
    RAISE EXCEPTION 'eval run % is finished and cannot change', OLD.id USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER eval_runs_frozen BEFORE UPDATE OR DELETE ON eval_runs FOR EACH ROW EXECUTE FUNCTION eval_runs_freeze();

CREATE TRIGGER llm_calls_append_only BEFORE UPDATE OR DELETE ON llm_calls
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER eval_items_append_only BEFORE UPDATE OR DELETE ON eval_items
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Entry points that run before a user id exists (DATA_MODEL 5.2). SECURITY DEFINER runs as app_owner, which is
-- exempt from row-level security, so the sign-in path can find a session or an account without opening the table.
CREATE FUNCTION resolve_session(p_token_sha256 bytea) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT s.user_id FROM sessions s JOIN users u ON u.id = s.user_id
  WHERE s.token_sha256 = p_token_sha256 AND s.revoked_at IS NULL AND s.expires_at > now() AND u.status = 'active'
$$;

-- Called after an emailed code is verified (PLT-02). New accounts start as plain users.
CREATE FUNCTION user_for_verified_email(p_email text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  uid uuid;
BEGIN
  SELECT id INTO uid FROM users WHERE lower(email) = lower(p_email);
  IF uid IS NULL THEN
    INSERT INTO users (email) VALUES (p_email) RETURNING id INTO uid;
  END IF;
  RETURN uid;
END;
$$;
"""

# DATA_MODEL 5.3. The API acts for one person per transaction; the worker serves everyone (USING true).
# Not here: outbox and queue_jobs. A sign-in code for a new address has no user yet, so its outbox row has no
# user_id and the API must be able to insert it; the API enqueues no user jobs in Phase 1.
RLS_SQL = """
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY self ON users TO app_api
  USING (id = app_user_id() AND app_agent_token_id() IS NULL) WITH CHECK (id = app_user_id());
CREATE POLICY worker_all ON users TO app_worker USING (true) WITH CHECK (true);

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON sessions TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON sessions TO app_worker USING (true) WITH CHECK (true);

ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON idempotency_keys TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON idempotency_keys TO app_worker USING (true) WITH CHECK (true);

-- The costs endpoint reads this under the caller's user id (plan 01-04); the gateway writes it as the worker.
ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON llm_calls TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON llm_calls TO app_worker USING (true) WITH CHECK (true);
"""

# DATA_MODEL 5.7, for the Phase 1 tables. The grant is the boundary; triggers are the second line.
GRANTS_SQL = """
GRANT USAGE ON SCHEMA public TO app_api, app_worker;

-- Public reference data: the job index, the model registry and eval runs.
GRANT SELECT ON job_sources, jobs, fetch_runs, model_registry, eval_runs TO app_api;

-- Append-only tables: read for the API, insert and read for the worker.
GRANT SELECT ON llm_calls TO app_api;
GRANT SELECT, INSERT ON fetch_runs, llm_calls, eval_items TO app_worker;
GRANT SELECT, INSERT, UPDATE ON eval_runs TO app_worker;

-- Mutable tables, scoped by the policies above.
GRANT SELECT, INSERT, UPDATE ON users TO app_api;
GRANT SELECT, INSERT ON queue_jobs TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON sessions, login_codes, outbox, idempotency_keys TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON users, job_sources, jobs, model_registry, queue_jobs TO app_worker;
"""

DOWNGRADE_SQL = """
REVOKE ALL ON job_sources, jobs, fetch_runs FROM app_api, app_worker;
DROP TABLE eval_items, outbox, idempotency_keys, queue_jobs, llm_calls, eval_runs, model_registry, login_codes,
           sessions, users;
DROP FUNCTION resolve_session(bytea), user_for_verified_email(text), eval_runs_freeze(), app_user_id(),
              app_agent_token_id(), forbid_mutation(), touch_updated_at();
DROP TYPE eval_status, eval_suite, queue_status, llm_outcome, model_kind, outbox_status, brief_frequency, user_status;
"""


def _role_exists(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}).scalar())


def _create_roles() -> None:
    """DATA_MODEL 5.1. A role that already exists (roles are cluster-wide) is left as it is."""
    if not _role_exists("app_owner"):
        op.execute("CREATE ROLE app_owner NOLOGIN")
    for name, (variable, development_default) in DEV_PASSWORDS.items():
        if _role_exists(name):
            continue
        password = os.environ.get(variable) or development_default
        # The raw psycopg connection joins the migration's transaction; sql.Literal quotes any password safely.
        statement = sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS PASSWORD {}").format(
            sql.Identifier(name), sql.Literal(password)
        )
        op.get_bind().connection.driver_connection.execute(statement)
    op.execute("GRANT app_owner TO CURRENT_USER")
    op.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_owner")
    op.execute("DO $$ BEGIN EXECUTE format('GRANT CREATE ON DATABASE %I TO app_owner', current_database()); END $$")


def upgrade() -> None:
    _create_roles()
    for table in JOB_TABLES:
        op.execute(f"ALTER TABLE {table} OWNER TO app_owner")
    # Every object below is created, and every grant issued, by its owner (ARCHITECTURE 7.1: app_owner owns everything).
    op.execute("SET ROLE app_owner")
    for statements in (TYPES_SQL, HELPERS_SQL, TABLES_SQL, AFTER_TABLES_SQL, RLS_SQL, GRANTS_SQL):
        op.execute(statements)
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
    for table in JOB_TABLES:
        op.execute(f"ALTER TABLE {table} OWNER TO CURRENT_USER")
