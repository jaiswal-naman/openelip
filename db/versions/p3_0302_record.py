"""Career Record: documents, the change log, facts, hard constraints and soft preferences (plan 03-04).

The DDL is copied from docs/DATA_MODEL.md: 2.1 types, 3.4 tables, 4.2 the span trigger, 4.5 the append-only and
write-once triggers (with write_once_columns(), which no earlier revision made), 5.3 and 5.7 row-level security and
grants.

Left out on purpose:
  * the users header (record_version, record_confirmed_at, record_embedding, record_embedding_model, and the CHECK that
    pairs the last two) already exists: 0002_platform made it with the table;
  * the foreign key from profile_facts.skill_id to skills: Phase 2 owns skills, so the phase that merges second adds it;
  * the app_match policies and grants: that role arrives with the match module.

work_arrangement, employment_type and seniority belong to the job index (Phase 2), but constraints use them, so each is
created here only if no other revision has made it. The downgrade leaves one in place while a job column still uses it.

Additive only. The downgrade drops what this revision made so the migration history round-trips on a scratch database;
it is never run against a database whose rows matter.
"""

import sqlalchemy as sa
from alembic import op

revision = "p3_0302_record"
down_revision = "p3_0301_trust"
branch_labels = None
depends_on = None

# DATA_MODEL 2.1. Each is created only if no earlier revision (or the other phase) made it.
TYPES_SQL = {
    "document_status": "CREATE TYPE document_status AS ENUM ('uploaded', 'parsing', 'parsed', 'failed')",
    "fact_kind": """CREATE TYPE fact_kind AS ENUM ('role', 'achievement', 'education', 'skill', 'certification',
                                  'project', 'other')""",
    "record_source": "CREATE TYPE record_source AS ENUM ('resume', 'candidate_edit', 'confirmed_proposal')",
    "constraint_kind": """CREATE TYPE constraint_kind AS ENUM ('location', 'remote_policy', 'work_authorization',
                                  'salary_floor', 'employment_type', 'seniority', 'excluded_company')""",
    "preference_kind": """CREATE TYPE preference_kind AS ENUM ('target_title', 'target_skill', 'industry',
                                  'company_size', 'location', 'remote_policy', 'salary_target', 'seniority',
                                  'untyped_dealbreaker', 'other')""",
    "profile_entity": "CREATE TYPE profile_entity AS ENUM ('fact', 'constraint', 'preference')",
    "profile_operation": "CREATE TYPE profile_operation AS ENUM ('create', 'update', 'remove', 'confirm', 'reject')",
    "profile_event_type": "CREATE TYPE profile_event_type AS ENUM ('proposed', 'applied', 'declined', 'expired')",
    "proposal_origin": "CREATE TYPE proposal_origin AS ENUM ('onboarding', 'chat', 'feedback', 'agent')",
    # Job-index types that the constraints table uses (Phase 2 creates them as well).
    "work_arrangement": "CREATE TYPE work_arrangement AS ENUM ('onsite', 'hybrid', 'remote')",
    "employment_type": """CREATE TYPE employment_type AS ENUM ('full_time', 'part_time', 'contract', 'internship',
                                  'temporary', 'unknown')""",
    "seniority": """CREATE TYPE seniority AS ENUM ('intern', 'entry', 'mid', 'senior', 'staff', 'principal', 'manager',
                                  'director', 'executive', 'unknown')""",
}

# DATA_MODEL 3.4, unchanged except the foreign key on profile_facts.skill_id (see above).
TABLES_SQL = """
CREATE TABLE documents (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  content           bytea           NOT NULL,            -- the uploaded file as a Fernet token
  original_filename text,
  mime_type         text            NOT NULL CHECK (mime_type IN (
                      'application/pdf',
                      'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
  byte_size         integer         NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),  -- assumption: 10 MB cap
  sha256            bytea           NOT NULL,
  status            document_status NOT NULL DEFAULT 'uploaded',
  extracted_text    text,                                -- visible text only; fact spans point into this
  hidden_text_chars integer         NOT NULL DEFAULT 0,  -- hidden or off-page characters stripped (REC-06)
  parse_error_code  text,
  created_at        timestamptz     NOT NULL DEFAULT now(),
  parsed_at         timestamptz,
  CHECK ((status = 'parsed') = (extracted_text IS NOT NULL AND parsed_at IS NOT NULL))
);
CREATE INDEX documents_user_idx ON documents (user_id, created_at DESC);

-- The change log and the proposal queue, append-only (REC-05, CHT-03).
CREATE TABLE profile_events (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid               NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_type           profile_event_type NOT NULL,
  entity               profile_entity     NOT NULL,
  operation            profile_operation  NOT NULL,
  entity_id            uuid,                 -- the row created or affected; NULL while only proposed
  before               jsonb,                -- NULL for a create
  after                jsonb,                -- NULL for a remove
  source               record_source,        -- 'applied' rows: who made the change
  origin               proposal_origin,      -- 'proposed' rows: where the proposal came from
  origin_ref           text,                 -- chat message id, feedback event id or agent action id
  proposal_event_id    uuid REFERENCES profile_events(id) ON DELETE CASCADE,  -- the proposal this row resolves
  reason               text,                 -- shown in the change log
  record_version_after bigint,               -- 'applied' rows: users.record_version after the change
  created_at           timestamptz NOT NULL DEFAULT now(),
  CHECK ((event_type = 'proposed') = (origin IS NOT NULL)),
  CHECK (event_type <> 'applied' OR (source IS NOT NULL AND record_version_after IS NOT NULL)),
  CHECK (source IS DISTINCT FROM 'confirmed_proposal' OR proposal_event_id IS NOT NULL),
  CHECK (event_type NOT IN ('declined', 'expired') OR proposal_event_id IS NOT NULL),
  CHECK (before IS NOT NULL OR after IS NOT NULL)
);
-- A proposal is resolved at most once (applied, declined or expired).
CREATE UNIQUE INDEX profile_events_one_resolution ON profile_events (proposal_event_id)
  WHERE proposal_event_id IS NOT NULL;
CREATE INDEX profile_events_log_idx ON profile_events (user_id, created_at DESC);

CREATE TABLE profile_facts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind             fact_kind     NOT NULL,
  statement        text          NOT NULL CHECK (length(statement) BETWEEN 1 AND 2000),
  attributes       jsonb         NOT NULL DEFAULT '{}',  -- typed per kind: employer, title, start_date, end_date ...
  skill_id         integer,                            -- taxonomy id for kind 'skill'; no foreign key yet (skills)
  parent_fact_id   uuid          REFERENCES profile_facts(id) ON DELETE CASCADE,  -- an achievement under a role
  source           record_source NOT NULL,
  document_id      uuid          REFERENCES documents(id) ON DELETE CASCADE,
  source_span      int4range,                          -- half-open character range in documents.extracted_text
  source_quote     text,                               -- the text at that range, checked on insert
  profile_event_id uuid          REFERENCES profile_events(id) ON DELETE CASCADE,  -- non-resume facts: where stated
  created_at       timestamptz   NOT NULL DEFAULT now(),
  -- write-once lifecycle columns
  confirmed_at     timestamptz,
  rejected_at      timestamptz,
  superseded_at    timestamptz,
  superseded_by    uuid          REFERENCES profile_facts(id) ON DELETE CASCADE,
  CHECK ((source = 'resume') = (document_id IS NOT NULL AND source_span IS NOT NULL AND source_quote IS NOT NULL)),
  CHECK (source = 'resume' OR profile_event_id IS NOT NULL),
  CHECK (source_span IS NULL OR (NOT isempty(source_span) AND lower(source_span) >= 0)),
  CHECK (NOT (confirmed_at IS NOT NULL AND rejected_at IS NOT NULL))
);
-- The facts anything may use: confirmed, not rejected, not superseded (REC-02).
CREATE INDEX profile_facts_usable_idx ON profile_facts (user_id, kind)
  WHERE confirmed_at IS NOT NULL AND rejected_at IS NULL AND superseded_at IS NULL;

-- Hard constraints (REC-03). One active row per kind; a change supersedes the row.
CREATE TABLE constraints (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind             constraint_kind NOT NULL,
  statement        text            NOT NULL,     -- the sentence the candidate confirmed
  country_codes    char(2)[],                    -- location, work_authorization: ISO 3166-1 alpha-2
  cities           text[],                       -- location, optional, lower case canonical names
  arrangements     work_arrangement[],           -- remote_policy: what the candidate accepts
  employment_types employment_type[],            -- employment_type
  seniorities      seniority[],                  -- seniority
  salary_floor     integer,                      -- salary_floor: annual, whole currency units
  salary_currency  char(3),
  company_names    text[],                       -- excluded_company, as the candidate typed them
  source           record_source   NOT NULL CHECK (source <> 'resume'),  -- never inferred from a resume
  profile_event_id uuid            NOT NULL REFERENCES profile_events(id) ON DELETE CASCADE,
  created_at       timestamptz     NOT NULL DEFAULT now(),
  superseded_at    timestamptz,                  -- write-once; NULL means active
  superseded_by    uuid            REFERENCES constraints(id) ON DELETE CASCADE,
  -- coalesce matters: a CHECK that evaluates to NULL passes
  CHECK (kind NOT IN ('location', 'work_authorization') OR coalesce(cardinality(country_codes), 0) > 0),
  CHECK (kind = 'location' OR cities IS NULL),
  CHECK (kind <> 'remote_policy'    OR coalesce(cardinality(arrangements), 0) > 0),
  CHECK (kind <> 'employment_type'  OR coalesce(cardinality(employment_types), 0) > 0),
  CHECK (NOT 'unknown' = ANY (coalesce(employment_types, '{}'))),
  CHECK (kind <> 'seniority'        OR coalesce(cardinality(seniorities), 0) > 0),
  CHECK (NOT 'unknown' = ANY (coalesce(seniorities, '{}'))),
  CHECK (kind <> 'salary_floor'     OR (coalesce(salary_floor, 0) > 0 AND salary_currency IS NOT NULL)),
  CHECK (kind <> 'excluded_company' OR coalesce(cardinality(company_names), 0) > 0)
);
CREATE UNIQUE INDEX constraints_one_active_per_kind ON constraints (user_id, kind) WHERE superseded_at IS NULL;

-- Soft preferences (REC-04). A dealbreaker that cannot be typed is stored here at weight -3.
CREATE TABLE preferences (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind             preference_kind NOT NULL,
  value            text            NOT NULL,     -- normalized token, for example 'staff engineer'
  skill_id         integer,
  weight           smallint        NOT NULL CHECK (weight BETWEEN -3 AND 3 AND weight <> 0),
  statement        text            NOT NULL,
  source           record_source   NOT NULL CHECK (source <> 'resume'),
  profile_event_id uuid            NOT NULL REFERENCES profile_events(id) ON DELETE CASCADE,
  created_at       timestamptz     NOT NULL DEFAULT now(),
  superseded_at    timestamptz,
  superseded_by    uuid            REFERENCES preferences(id) ON DELETE CASCADE,
  CHECK (kind <> 'untyped_dealbreaker' OR weight = -3)
);
CREATE INDEX preferences_active_idx ON preferences (user_id, kind) WHERE superseded_at IS NULL;
"""

# DATA_MODEL 3.2 (write_once_columns), 4.2 (the span trigger) and 4.5 (the triggers). forbid_mutation() comes from
# 0002_platform. Erasure sets app.maintenance, which lets these triggers through for that transaction only.
TRIGGERS_SQL = """
-- Versioned rows: content is immutable; only the named lifecycle columns may change, and only from NULL.
CREATE FUNCTION write_once_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  allowed text[] := TG_ARGV;
  o jsonb := to_jsonb(OLD);
  n jsonb := to_jsonb(NEW);
  col text;
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN NEW;
  END IF;
  IF (n - allowed) IS DISTINCT FROM (o - allowed) THEN
    RAISE EXCEPTION '% rows are immutable except for %', TG_TABLE_NAME, allowed
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  FOREACH col IN ARRAY allowed LOOP
    IF (o -> col) <> 'null'::jsonb AND (n -> col) IS DISTINCT FROM (o -> col) THEN
      RAISE EXCEPTION '%.% can be set only once', TG_TABLE_NAME, col
        USING ERRCODE = 'insufficient_privilege';
    END IF;
  END LOOP;
  RETURN NEW;
END;
$$;

-- Invariant 2. Spans: a stored quote must be the text at its span.
CREATE FUNCTION profile_facts_check_span() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  src text;
BEGIN
  IF NEW.document_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT d.extracted_text INTO src FROM documents d WHERE d.id = NEW.document_id AND d.user_id = NEW.user_id;
  IF src IS NULL OR substr(src, lower(NEW.source_span) + 1, upper(NEW.source_span) - lower(NEW.source_span))
                    IS DISTINCT FROM NEW.source_quote THEN
    RAISE EXCEPTION 'span_unresolved: fact quote is not the document text at its span'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER profile_facts_span BEFORE INSERT ON profile_facts
  FOR EACH ROW EXECUTE FUNCTION profile_facts_check_span();

CREATE TRIGGER profile_events_append_only BEFORE UPDATE OR DELETE ON profile_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER profile_facts_write_once BEFORE UPDATE ON profile_facts
  FOR EACH ROW EXECUTE FUNCTION write_once_columns('confirmed_at', 'rejected_at', 'superseded_at', 'superseded_by');
CREATE TRIGGER constraints_write_once   BEFORE UPDATE ON constraints
  FOR EACH ROW EXECUTE FUNCTION write_once_columns('superseded_at', 'superseded_by');
CREATE TRIGGER preferences_write_once   BEFORE UPDATE ON preferences
  FOR EACH ROW EXECUTE FUNCTION write_once_columns('superseded_at', 'superseded_by');
CREATE TRIGGER profile_facts_no_delete  BEFORE DELETE ON profile_facts FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER constraints_no_delete    BEFORE DELETE ON constraints   FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER preferences_no_delete    BEFORE DELETE ON preferences   FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
"""

# DATA_MODEL 5.3: the API acts for one person per transaction; the worker serves everyone (USING true).
RECORD_TABLES = ("documents", "profile_events", "profile_facts", "constraints", "preferences")
RLS_SQL = "".join(
    f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
CREATE POLICY candidate_own ON {table} TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY worker_all ON {table} TO app_worker USING (true) WITH CHECK (true);
"""
    for table in RECORD_TABLES
)

# DATA_MODEL 5.7. The grant is the boundary; the triggers are the second line.
GRANTS_SQL = """
GRANT SELECT, INSERT ON profile_events TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE ON profile_facts, constraints, preferences TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO app_api, app_worker;
"""

DOWNGRADE_SQL = """
DROP TABLE profile_facts, constraints, preferences, profile_events, documents;
DROP FUNCTION profile_facts_check_span(), write_once_columns();
DROP TYPE proposal_origin, profile_event_type, profile_operation, profile_entity, preference_kind, constraint_kind,
          record_source, fact_kind, document_status;
-- Shared with the job index: dropped only when nothing else uses them.
DO $$
BEGIN
  DROP TYPE work_arrangement, employment_type, seniority;
EXCEPTION WHEN dependent_objects_still_exist THEN
  NULL;
END;
$$;
"""


def _type_exists(name: str) -> bool:
    return bool(op.get_bind().execute(sa.text(f"SELECT to_regtype('{name}') IS NOT NULL")).scalar())


def upgrade() -> None:
    # Everything is created, and every grant issued, by app_owner (ARCHITECTURE 7.1), as in 0002_platform.
    op.execute("SET ROLE app_owner")
    for name, create in TYPES_SQL.items():
        if not _type_exists(name):
            op.execute(create)
    for statements in (TABLES_SQL, TRIGGERS_SQL, RLS_SQL, GRANTS_SQL):
        op.execute(statements)
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
