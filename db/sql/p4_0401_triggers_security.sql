-- Revision p4_0401_match_brief, part 3: the guards that keep the decision ledger true, row-level security, grants, and the
-- 90-day retention step. Read by db/versions/p4_0401_match_brief.py, which runs it as app_owner (so every object below is
-- owned by app_owner and every grant is issued by its owner, ARCHITECTURE 7.1). DATA_MODEL 4.2, 4.5, 5.3, 5.6, 5.7, 7.1.
--
-- Not here on purpose: the agent_matches and agent_explanations policies (outside-agent scopes, Phase 9) and the grants of
-- tables that do not exist yet. forbid_mutation() comes from 0002_platform.

-- DATA_MODEL 4.2, invariant 2. The citation check: an evidence row is accepted only if both sides resolve (MAT-05).
CREATE FUNCTION match_evidence_check_citations() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  resolved integer;
BEGIN
  -- Job side: the requirement belongs to the decision's job at the decision's content version,
  -- that version is still the job's text, and the requirement's span still reads as its text.
  PERFORM 1
  FROM match_decisions d
  JOIN jobs j             ON j.id = d.job_id AND j.content_version = d.job_content_version
  JOIN job_requirements r ON r.job_id = j.id AND r.content_version = j.content_version
  WHERE d.id = NEW.match_decision_id
    AND d.user_id = NEW.user_id
    AND r.id = NEW.job_requirement_id
    AND substr(j.description, lower(r.span) + 1, upper(r.span) - lower(r.span)) = r.text;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'citation_unresolved: job side' USING ERRCODE = 'check_violation';
  END IF;

  -- Candidate side: every cited fact is this candidate's, confirmed, not rejected, not superseded.
  SELECT count(*) INTO resolved
  FROM profile_facts f
  WHERE f.id = ANY (NEW.fact_ids)
    AND f.user_id = NEW.user_id
    AND f.confirmed_at IS NOT NULL
    AND f.rejected_at IS NULL
    AND f.superseded_at IS NULL;
  IF resolved <> cardinality(NEW.fact_ids) THEN      -- a repeated id also fails here, on purpose
    RAISE EXCEPTION 'citation_unresolved: candidate side' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER match_evidence_citations BEFORE INSERT ON match_evidence
  FOR EACH ROW EXECUTE FUNCTION match_evidence_check_citations();

-- A shown or introduced match must carry a verdict for every requirement of its job version (MAT-03).
-- Checked at commit, so the decision and its evidence rows are written in one transaction.
CREATE FUNCTION match_decisions_check_complete() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  required integer;
  present  integer;
BEGIN
  IF NEW.outcome NOT IN ('shown', 'introduced') THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO required FROM job_requirements r
  WHERE r.job_id = NEW.job_id AND r.content_version = NEW.job_content_version;
  SELECT count(*) INTO present FROM match_evidence e WHERE e.match_decision_id = NEW.id;
  IF required = 0 OR present <> required THEN
    RAISE EXCEPTION 'evidence_incomplete: % of % requirements have a verdict', present, required
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER match_decisions_complete AFTER INSERT ON match_decisions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION match_decisions_check_complete();

-- DATA_MODEL 4.5. Append-only: rows leave these tables only through erase_user and purge_expired.
CREATE TRIGGER match_decisions_append_only    BEFORE UPDATE OR DELETE ON match_decisions    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER match_evidence_append_only     BEFORE UPDATE OR DELETE ON match_evidence     FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER feedback_events_append_only    BEFORE UPDATE OR DELETE ON feedback_events    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- DATA_MODEL 5.3. Candidate isolation: the API acts for one person per transaction (an outside agent has no policy here
-- yet), and the worker serves every person.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['match_runs', 'match_decisions', 'match_evidence', 'feedback_events', 'briefs', 'brief_items'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY candidate_own ON %I TO app_api
         USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
         WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL)', t);
    EXECUTE format('CREATE POLICY worker_all ON %I TO app_worker USING (true) WITH CHECK (true)', t);
  END LOOP;
END;
$$;

-- DATA_MODEL 5.6. The match module reads only what matching may use: confirmed facts and active record rows. The
-- policies on queue_jobs take effect once row-level security is enabled there; until then the grant is the boundary.
CREATE POLICY match_read  ON users           FOR SELECT TO app_match USING (status = 'active');
CREATE POLICY match_read  ON profile_facts   FOR SELECT TO app_match
  USING (confirmed_at IS NOT NULL AND rejected_at IS NULL AND superseded_at IS NULL);
CREATE POLICY match_read  ON constraints     FOR SELECT TO app_match USING (superseded_at IS NULL);
CREATE POLICY match_read  ON preferences     FOR SELECT TO app_match USING (superseded_at IS NULL);
CREATE POLICY match_read  ON feedback_events FOR SELECT TO app_match USING (true);
CREATE POLICY match_write ON match_runs      TO app_match USING (true) WITH CHECK (true);
CREATE POLICY match_write ON match_decisions TO app_match USING (true) WITH CHECK (true);
CREATE POLICY match_write ON match_evidence  TO app_match USING (true) WITH CHECK (true);
CREATE POLICY match_write ON llm_calls       TO app_match USING (true) WITH CHECK (true);
CREATE POLICY match_write ON queue_jobs      TO app_match USING (true) WITH CHECK (true);

-- DATA_MODEL 5.7. The grant is the boundary; triggers are the second line.
GRANT USAGE ON SCHEMA public TO app_match;

-- A candidate reads what the worker wrote and adds their own saves and dismissals; the ledger itself is not theirs.
GRANT SELECT ON match_runs, match_decisions, match_evidence TO app_api;
GRANT SELECT, INSERT ON feedback_events TO app_api;
GRANT SELECT, INSERT ON match_decisions, match_evidence, feedback_events TO app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON match_runs TO app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON briefs, brief_items TO app_api, app_worker;

-- The match module: the job index, the usable record, and its own ledger. Nothing on documents, consents, chat or the
-- hiring side, and only the columns of users that matching reads.
GRANT SELECT ON jobs, job_requirements, skills TO app_match;
GRANT SELECT (id, status, timezone, brief_frequency, brief_weekday, is_synthetic,
              record_version, record_confirmed_at, record_embedding) ON users TO app_match;
GRANT SELECT ON profile_facts, constraints, preferences, feedback_events TO app_match;
GRANT SELECT, INSERT, UPDATE ON match_runs, queue_jobs TO app_match;
GRANT SELECT, INSERT ON match_decisions, match_evidence, llm_calls TO app_match;

-- DATA_MODEL 7.1, the retention sweep. Revision p3_0301_trust made it with only the export-archive step; this adds the
-- match_decisions step and, as 7.1 does, sets app.maintenance first: without it the append-only trigger would refuse the
-- delete. CREATE OR REPLACE keeps the owner and the EXECUTE grant (the worker only) that p3_0301_trust gave it.
CREATE OR REPLACE FUNCTION purge_expired() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  result jsonb := '{}';
  n integer;
BEGIN
  PERFORM set_config('app.maintenance', 'on', true);
  DELETE FROM match_decisions WHERE outcome IN ('withheld', 'filtered') AND created_at < now() - interval '90 days';
  GET DIAGNOSTICS n = ROW_COUNT; result := result || jsonb_build_object('match_decisions', n);
  UPDATE data_requests SET export_archive = NULL
   WHERE export_archive IS NOT NULL AND export_expires_at < now();
  GET DIAGNOSTICS n = ROW_COUNT; result := result || jsonb_build_object('export_links', n);
  RETURN result;
END;
$$;
