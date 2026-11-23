-- Revision p2_0201_job_index, part 1: the job-index enum types, then DATA_MODEL 8 step 2, which converts job_sources,
-- jobs and fetch_runs in place. Read by db/versions/p2_0201_job_index.py, which runs it as app_owner.
--
-- Differences from DATA_MODEL 8, all on purpose:
--   * The three OWNER TO lines are left out: revision 0002_platform already moved the tables to app_owner.
--   * The backfill UPDATE is not in this file. The revision runs it at the marker line below, with the embedding model
--     name bound as a parameter, because psycopg cannot bind a parameter inside a multi-statement string.
--   * Each CREATE TYPE skips a type that already exists, so a database that got one earlier still upgrades.

-- DATA_MODEL 3.1, job index.
DO $$ BEGIN
  CREATE TYPE ats_type AS ENUM ('greenhouse', 'lever', 'ashby', 'direct');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE fetch_status AS ENUM ('ok', 'http_error', 'parse_error', 'suspicious_empty', 'network_error');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('open', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE work_arrangement AS ENUM ('onsite', 'hybrid', 'remote');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE employment_type AS ENUM ('full_time', 'part_time', 'contract', 'internship', 'temporary', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE seniority AS ENUM ('intern', 'entry', 'mid', 'senior', 'staff', 'principal', 'manager', 'director',
                                 'executive', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE visa_sponsorship AS ENUM ('offered', 'not_offered', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE requirement_importance AS ENUM ('required', 'preferred');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE TYPE requirement_kind AS ENUM ('skill', 'experience', 'education', 'certification', 'authorization',
                                        'location', 'other');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Two suites for this phase. requirement_extraction is already in the type from revision 0002_platform, and the
-- IF NOT EXISTS makes this a no-op for it. location_accuracy is new (IDX-06); the new value cannot be used until this
-- transaction commits, and nothing here uses it.
ALTER TYPE eval_suite ADD VALUE IF NOT EXISTS 'location_accuracy';
ALTER TYPE eval_suite ADD VALUE IF NOT EXISTS 'requirement_extraction';

-- DATA_MODEL 8, step 2.
ALTER TABLE job_sources DROP CONSTRAINT job_sources_ats_check;
ALTER TABLE job_sources
  ALTER COLUMN ats TYPE ats_type USING ats::ats_type,
  ALTER COLUMN last_status TYPE fetch_status USING last_status::fetch_status,
  ADD COLUMN organization_id uuid,
  ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0,
  ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(),
  ADD CHECK ((ats = 'direct') = (organization_id IS NOT NULL));

ALTER TABLE fetch_runs ALTER COLUMN status TYPE fetch_status USING status::fetch_status;

ALTER TABLE jobs DROP CONSTRAINT jobs_status_check;
DROP INDEX jobs_open_idx, jobs_dedupe_idx;            -- their predicates compare status with text; rebuilt below
ALTER TABLE jobs RENAME COLUMN employment_type TO employment_type_raw;
ALTER TABLE jobs
  ALTER COLUMN status DROP DEFAULT,
  ALTER COLUMN status TYPE job_status USING status::job_status,
  ALTER COLUMN status SET DEFAULT 'open',
  ADD COLUMN work_arrangements work_arrangement[] NOT NULL DEFAULT '{}',
  ADD COLUMN employment_type employment_type NOT NULL DEFAULT 'unknown',
  ADD COLUMN seniority seniority NOT NULL DEFAULT 'unknown',
  ADD COLUMN salary_min integer,
  ADD COLUMN salary_max integer,
  ADD COLUMN salary_currency char(3),
  ADD COLUMN visa_sponsorship visa_sponsorship NOT NULL DEFAULT 'unknown',
  ADD COLUMN change_note text,
  ADD COLUMN materially_changed_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN is_listed boolean NOT NULL DEFAULT true,
  ADD COLUMN apply_checked_at timestamptz,
  ADD COLUMN apply_check_ok boolean,
  ADD COLUMN requirements_version integer,
  ADD COLUMN embedding_model text;

-- @@BACKFILL@@
-- The revision splits this file at the marker line above and runs the jobs backfill there (work_arrangements,
-- employment_type, materially_changed_at, closed_at, embedding_model), then continues with the rest of the file.

ALTER TABLE jobs DROP COLUMN is_remote;

ALTER TABLE jobs
  ADD CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
  ADD CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max),
  ADD CHECK ((salary_min IS NULL AND salary_max IS NULL) OR salary_currency IS NOT NULL),
  ADD CHECK (requirements_version IS NULL OR requirements_version <= content_version),
  ADD CHECK ((embedding IS NULL) = (embedding_model IS NULL)),
  ADD CHECK (canonical_job_id IS DISTINCT FROM id);
CREATE INDEX jobs_open_idx   ON jobs (status, country) WHERE status = 'open';
CREATE INDEX jobs_dedupe_idx ON jobs (dedupe_key) WHERE status = 'open';
-- Approximate nearest neighbour over canonical open jobs; cosine matches normalized text embeddings.
CREATE INDEX jobs_embedding_hnsw ON jobs USING hnsw (embedding vector_cosine_ops)
  WHERE status = 'open' AND canonical_job_id IS NULL;
-- Nightly runs: "open jobs new or changed since the last run" is a range scan here.
CREATE INDEX jobs_changed_idx ON jobs (materially_changed_at) WHERE status = 'open';
CREATE INDEX fetch_runs_source_idx ON fetch_runs (source_id, started_at DESC);
