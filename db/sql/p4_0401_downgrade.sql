-- Revision p4_0401_match_brief, downgrade: back to the shape revision p3_0304_audit_subject leaves. Read by the revision, which
-- runs it as app_owner.
--
-- It exists so the CI migration check (up, down to 0001_baseline, up) can run on a scratch database. It is lossy by nature:
-- the decisions, their evidence, the feedback and the briefs are dropped. Never run it against a database whose rows
-- matter. The login role app_match is left alone: roles belong to the whole server, and other databases may use it.
--
-- The first statement refuses the live database, which is called app. The migration check in CI and the tests use other
-- names (migration_check, names ending in _test).

DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

-- What this revision added to tables that stay: the match module's policies and its grants.
DROP POLICY match_read ON users;
DROP POLICY match_read ON profile_facts;
DROP POLICY match_read ON constraints;
DROP POLICY match_read ON preferences;
DROP POLICY match_write ON llm_calls;
DROP POLICY match_write ON queue_jobs;
REVOKE ALL ON users, jobs, job_requirements, skills, profile_facts, constraints, preferences, llm_calls, queue_jobs
  FROM app_match;
REVOKE USAGE ON SCHEMA public FROM app_match;

-- The retention sweep as revision p3_0301_trust made it: the export archives only.
CREATE OR REPLACE FUNCTION purge_expired() RETURNS jsonb
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

-- The tables, with their triggers, indexes, policies and grants, then the trigger functions that served them.
DROP TABLE brief_items, briefs, feedback_events, match_evidence, match_decisions, match_runs;
DROP FUNCTION match_decisions_check_complete(), match_evidence_check_citations();

-- The constraint filter and the company-name helpers, then the types, which nothing uses any more.
DROP FUNCTION unverified_fields(jobs, compiled_constraints), constraint_violations(jobs, compiled_constraints),
              compile_constraints(uuid), company_keys(text[]), company_key(text);
DROP TYPE compiled_constraints;
DROP TYPE brief_item_kind, brief_status, brief_channel, dismiss_reason, feedback_kind, evidence_verdict,
          unverified_flag, match_reason, match_outcome, match_run_status, match_trigger;
