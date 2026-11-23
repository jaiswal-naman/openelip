-- Revision p2_0201_job_index, downgrade: back to the shape revision 0002_platform leaves. Read by the revision, which
-- runs it as app_owner.
--
-- It exists so the CI migration check (up, down to 0001_baseline, up) can run on a scratch database. It is lossy by
-- nature: the requirement and skill rows are dropped, and is_remote is rebuilt from work_arrangements. Never run it
-- against a database whose rows matter. eval_suite keeps the location_accuracy value, because PostgreSQL cannot remove
-- an enum value; the upgrade skips a value that already exists.
--
-- The first statement refuses the live database, which is called app. The migration check in CI and the tests use
-- other names (migration_check, names ending in _test).

DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

DROP TRIGGER jobs_version_guard ON jobs;
DROP TRIGGER fetch_runs_append_only ON fetch_runs;
DROP TABLE job_requirements, skills;
DROP FUNCTION job_requirements_check_span(), jobs_version_guard();

DROP INDEX fetch_runs_source_idx;
ALTER TABLE fetch_runs ALTER COLUMN status TYPE text USING status::text;

-- Every CHECK on job_sources and jobs was added by the upgrade, except the one each had in the baseline. All go; those
-- two are put back below.
DO $$
DECLARE
  c record;
BEGIN
  FOR c IN SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
            WHERE conrelid IN ('job_sources'::regclass, 'jobs'::regclass) AND contype = 'c' LOOP
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
  END LOOP;
END $$;

ALTER TABLE job_sources
  ALTER COLUMN ats TYPE text USING ats::text,
  ALTER COLUMN last_status TYPE text USING last_status::text,
  DROP COLUMN organization_id,
  DROP COLUMN consecutive_failures,
  DROP COLUMN created_at,
  ADD CONSTRAINT job_sources_ats_check CHECK (ats IN ('greenhouse', 'lever', 'ashby'));

DROP INDEX jobs_open_idx, jobs_dedupe_idx, jobs_embedding_hnsw, jobs_changed_idx;
ALTER TABLE jobs ADD COLUMN is_remote boolean;
UPDATE jobs SET is_remote = CASE WHEN 'remote' = ANY(work_arrangements) THEN true
                                 WHEN cardinality(work_arrangements) = 0 THEN NULL
                                 ELSE false END;
ALTER TABLE jobs
  DROP COLUMN work_arrangements,
  DROP COLUMN employment_type,
  DROP COLUMN seniority,
  DROP COLUMN salary_min,
  DROP COLUMN salary_max,
  DROP COLUMN salary_currency,
  DROP COLUMN visa_sponsorship,
  DROP COLUMN change_note,
  DROP COLUMN materially_changed_at,
  DROP COLUMN is_listed,
  DROP COLUMN apply_checked_at,
  DROP COLUMN apply_check_ok,
  DROP COLUMN requirements_version,
  DROP COLUMN embedding_model;
ALTER TABLE jobs RENAME COLUMN employment_type_raw TO employment_type;
ALTER TABLE jobs
  ALTER COLUMN status DROP DEFAULT,
  ALTER COLUMN status TYPE text USING status::text,
  ALTER COLUMN status SET DEFAULT 'open',
  ADD CONSTRAINT jobs_status_check CHECK (status IN ('open', 'closed'));
CREATE INDEX jobs_open_idx   ON jobs (status, country) WHERE status = 'open';
CREATE INDEX jobs_dedupe_idx ON jobs (dedupe_key) WHERE status = 'open';

DROP TYPE ats_type, fetch_status, job_status, work_arrangement, employment_type, seniority, visa_sponsorship,
          requirement_importance, requirement_kind;
