-- Revision p5_0501_chat_tailor_apply, part 2: the guards that keep a tailored resume true, the append-only status history,
-- row-level security and grants. Read by db/versions/p5_0501_chat_tailor_apply.py, which runs it as app_owner (so every
-- object below is owned by app_owner and every grant is issued by its owner, ARCHITECTURE 7.1). DATA_MODEL 4.3, 4.5, 5.3
-- and 5.7. forbid_mutation() comes from 0002_platform.
--
-- The other half of invariant 3 (a tailored edit may rest only on the person's usable facts) is application code in
-- app.tailor; this file is the half the database enforces whatever the code does (TLR-02).

-- DATA_MODEL 4.3, invariant 3. A passed edit can only be supported by the candidate's usable facts. A discarded edit has no
-- support to check. A repeated fact id fails here on purpose, as it does for a citation.
CREATE FUNCTION resume_edits_check_support() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  resolved integer;
BEGIN
  IF NEW.verifier_status = 'discarded' THEN
    RETURN NEW;
  END IF;
  SELECT count(*) INTO resolved FROM profile_facts f
  WHERE f.id = ANY (NEW.supporting_fact_ids) AND f.user_id = NEW.user_id
    AND f.confirmed_at IS NOT NULL AND f.rejected_at IS NULL AND f.superseded_at IS NULL;
  IF resolved <> cardinality(NEW.supporting_fact_ids) THEN
    RAISE EXCEPTION 'unsupported_edit: a supporting fact is not a confirmed fact of this candidate'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER resume_edits_support BEFORE INSERT ON resume_edits
  FOR EACH ROW EXECUTE FUNCTION resume_edits_check_support();

-- An edit's content is fixed once written; the candidate's decision is the one thing that changes, and only once.
-- Phase 5: DATA_MODEL 4.3 used write_once_columns('decision', 'decided_at') here. That function refuses any change to a
-- column that is not NULL, and decision starts as 'pending', so it would refuse the first decision. This one lets the
-- decision leave 'pending' once. The table's CHECKs still keep a discarded edit pending and tie decided_at to the
-- decision. Like write_once_columns it lets app.maintenance through (erase_user and purge_expired set it).
CREATE FUNCTION resume_edits_decide_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN NEW;
  END IF;
  IF (to_jsonb(NEW) - ARRAY['decision', 'decided_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['decision', 'decided_at']) THEN
    RAISE EXCEPTION '% rows are immutable except for decision and decided_at', TG_TABLE_NAME
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.decision <> 'pending'
     AND (NEW.decision IS DISTINCT FROM OLD.decision OR NEW.decided_at IS DISTINCT FROM OLD.decided_at) THEN
    RAISE EXCEPTION '%.decision can be changed only once', TG_TABLE_NAME
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER resume_edits_content_fixed BEFORE UPDATE ON resume_edits
  FOR EACH ROW EXECUTE FUNCTION resume_edits_decide_once();

-- DATA_MODEL 4.5. Append-only: rows leave this table only through erase_user.
CREATE TRIGGER application_events_append_only BEFORE UPDATE OR DELETE ON application_events FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- DATA_MODEL 5.3. Candidate isolation: the API acts for one person per transaction (an outside agent has no policy here),
-- and the worker serves every person.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tailored_resumes', 'resume_edits', 'application_packets', 'applications',
                           'application_events', 'chat_sessions', 'chat_messages'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY candidate_own ON %I TO app_api
         USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
         WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL)', t);
    EXECUTE format('CREATE POLICY worker_all ON %I TO app_worker USING (true) WITH CHECK (true)', t);
  END LOOP;
END;
$$;

-- DATA_MODEL 5.7. The grant is the boundary; triggers are the second line. The status history is insert and read only.
GRANT SELECT, INSERT ON application_events TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON tailored_resumes, resume_edits, application_packets, applications,
                                        chat_sessions, chat_messages
  TO app_api, app_worker;
-- The chat reply calls the model inside the API process, which holds no worker login, so the API login records those
-- calls. The candidate_own policy of llm_calls (0002) already limits such a row to the signed-in person. On
-- model_registry it can add the row of a model the code names on first use; it cannot change or remove one.
GRANT INSERT ON llm_calls, model_registry TO app_api;
