"""Baseline: the job index exactly as db/schema.sql defined it before Alembic (DATA_MODEL 8, step 1).

The statements are all IF NOT EXISTS, so on the live database this revision is stamped rather than run, and running
it there would change nothing. On an empty database it builds the old shape; both paths then share one history.
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SQL = """-- Job index schema. Idempotent: safe to run on every start.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS job_sources (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ats             text NOT NULL CHECK (ats IN ('greenhouse', 'lever', 'ashby')),
  token           text NOT NULL,
  company_name    text NOT NULL,
  is_active       boolean NOT NULL DEFAULT true,
  last_fetched_at timestamptz,
  last_status     text,
  UNIQUE (ats, token)
);

CREATE TABLE IF NOT EXISTS jobs (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id        bigint NOT NULL REFERENCES job_sources (id),
  external_id      text NOT NULL,
  title            text NOT NULL,
  company_name     text NOT NULL,
  location_raw     text,
  country          char(2),          -- ISO 3166-1 alpha-2 when recognised, else NULL (unknown, never guessed)
  city             text,
  is_remote        boolean,          -- NULL = the posting does not say
  employment_type  text,
  department       text,
  description      text NOT NULL,
  apply_url        text NOT NULL,
  posted_at        timestamptz,
  content_hash     text NOT NULL,
  content_version  int NOT NULL DEFAULT 1,
  dedupe_key       text NOT NULL,
  canonical_job_id bigint REFERENCES jobs (id),
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  first_seen_at    timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  closed_at        timestamptz,
  search           tsvector GENERATED ALWAYS AS (
                     setweight(to_tsvector('english', title), 'A') ||
                     setweight(to_tsvector('english', coalesce(department, '')), 'B') ||
                     setweight(to_tsvector('english', left(description, 20000)), 'C')
                   ) STORED,
  embedding        vector(384),
  UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS jobs_search_idx  ON jobs USING gin (search);
CREATE INDEX IF NOT EXISTS jobs_open_idx    ON jobs (status, country) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS jobs_dedupe_idx  ON jobs (dedupe_key) WHERE status = 'open';

-- One row per fetch attempt, success or failure: the verification history.
CREATE TABLE IF NOT EXISTS fetch_runs (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id    bigint NOT NULL REFERENCES job_sources (id),
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL,     -- ok | http_error | parse_error | suspicious_empty | network_error
  http_status  int,
  jobs_seen    int NOT NULL DEFAULT 0,
  jobs_new     int NOT NULL DEFAULT 0,
  jobs_changed int NOT NULL DEFAULT 0,
  jobs_closed  int NOT NULL DEFAULT 0,
  error        text
);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(SCHEMA_SQL)


def downgrade() -> None:
    raise NotImplementedError("0001 is the baseline: going below it would drop the job index and its rows")
