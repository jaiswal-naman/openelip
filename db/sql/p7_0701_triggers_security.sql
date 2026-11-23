-- Revision p7_0701_campaigns_guardian, part 2: the append-only and two-key triggers, record_state_change, row-level
-- security and grants. DATA_MODEL 4.5 (guardian_checks_append_only), 4.6, 5.3, 5.7. Read by the revision, which runs it as
-- app_owner.
--
-- Differences from the document (decisions 3 and 31):
--   agent_actions_guard    undo_until may change exactly once: from NULL, on the move to scheduled, to at least one minute
--                          ahead; every other content column stays immutable
--   record_state_change    new SECURITY DEFINER function; the only way the API login writes into agent_actions, and only a
--                          state_change row on the caller's own campaign
-- Left out on purpose: the twin_messages trigger and policies (Phase 9).

-- 4.5 ------------------------------------------------------------------------------------------
CREATE TRIGGER guardian_checks_append_only BEFORE UPDATE OR DELETE ON guardian_checks
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- 4.6 ------------------------------------------------------------------------------------------
-- Invariant 5. The two-key rule for outside actions.

-- Actions that always need the candidate's approval (GRD-01): a packet and an email to an outside recruiter.
CREATE FUNCTION action_always_needs_approval(t action_type) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT t IN ('submit_application', 'send_follow_up')
$$;

ALTER TABLE autonomy_settings ADD CONSTRAINT autonomy_never_automatic
  CHECK (NOT (action_always_needs_approval(action_type) AND level = 'tell_me_after'));

CREATE FUNCTION agent_actions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  dial autonomy_level;
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'agent_actions is the flight recorder; rows are never deleted' USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('recorded', 'proposed') THEN
      RAISE EXCEPTION 'an action is recorded first and moves forward by update' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- UPDATE: content is fixed; only status, executed_at, outcome_code and the one-time undo_until move.
  IF (to_jsonb(NEW) - ARRAY['status', 'executed_at', 'outcome_code', 'undo_until'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'executed_at', 'outcome_code', 'undo_until']) THEN
    RAISE EXCEPTION 'agent_actions content is immutable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- undo_until is set once, from NULL, on the move to scheduled, and then ahead of now by at least a minute (decision 3).
  IF NEW.undo_until IS DISTINCT FROM OLD.undo_until THEN
    IF OLD.undo_until IS NOT NULL THEN
      RAISE EXCEPTION 'undo_until is set once and never changes' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> 'scheduled' OR OLD.status NOT IN ('proposed', 'awaiting_approval') THEN
      RAISE EXCEPTION 'undo_until is set only when the action is scheduled' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.undo_until IS NULL OR NEW.undo_until < now() + interval '1 minute' THEN
      RAISE EXCEPTION 'undo_until must be at least one minute ahead' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;
  IF NOT (OLD.status, NEW.status) IN (
       ('proposed', 'awaiting_approval'), ('proposed', 'scheduled'), ('proposed', 'blocked'),
       ('awaiting_approval', 'scheduled'), ('awaiting_approval', 'rejected'),
       ('awaiting_approval', 'expired'), ('awaiting_approval', 'cancelled'), ('awaiting_approval', 'blocked'),
       ('scheduled', 'executed'), ('scheduled', 'failed'), ('scheduled', 'cancelled'), ('scheduled', 'blocked')) THEN
    RAISE EXCEPTION 'agent action cannot move from % to %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;

  -- Both keys are checked when the action is cleared to run and again when it runs.
  IF NEW.status IN ('scheduled', 'executed') THEN
    IF NEW.policy_decision = 'deny' THEN
      RAISE EXCEPTION 'two_key: the policy engine denied this action' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM guardian_checks g WHERE g.agent_action_id = NEW.id)
       OR EXISTS (SELECT 1 FROM guardian_checks g WHERE g.agent_action_id = NEW.id AND NOT g.passed) THEN
      RAISE EXCEPTION 'two_key: the Guardian has not passed this action' USING ERRCODE = 'check_violation';
    END IF;
    -- The autonomy dial is read again here, so a policy engine bug cannot skip it. No row = 'ask_first'.
    SELECT s.level INTO dial FROM autonomy_settings s
    WHERE s.user_id = NEW.user_id AND s.action_type = NEW.action_type;
    dial := coalesce(dial, 'ask_first');
    IF dial = 'never' THEN
      RAISE EXCEPTION 'two_key: the candidate set % to never', NEW.action_type USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.policy_decision = 'ask' OR action_always_needs_approval(NEW.action_type) OR dial = 'ask_first')
       AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.agent_action_id = NEW.id AND a.status = 'approved') THEN
      RAISE EXCEPTION 'two_key: the candidate has not approved this action' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.status = 'executed' AND NEW.undo_until IS NOT NULL AND now() < NEW.undo_until THEN
    RAISE EXCEPTION 'undo window is still open until %', NEW.undo_until USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER agent_actions_guard BEFORE INSERT OR UPDATE OR DELETE ON agent_actions
  FOR EACH ROW EXECUTE FUNCTION agent_actions_guard();

-- An approval is decided once, before it expires, and never deleted.
CREATE FUNCTION approvals_decide_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' OR OLD.status <> 'pending'
     OR (NEW.agent_action_id, NEW.user_id, NEW.requested_at, NEW.expires_at)
        IS DISTINCT FROM (OLD.agent_action_id, OLD.user_id, OLD.requested_at, OLD.expires_at) THEN
    RAISE EXCEPTION 'an approval is decided once and then kept' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.status = 'approved' AND now() >= OLD.expires_at THEN
    RAISE EXCEPTION 'approval expired at %', OLD.expires_at USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER approvals_decide_once BEFORE UPDATE OR DELETE ON approvals
  FOR EACH ROW EXECUTE FUNCTION approvals_decide_once();

-- An email that is an agent's outside action is queued only once both keys have passed.
CREATE FUNCTION outbox_check_action() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NEW.agent_action_id IS NOT NULL THEN
    PERFORM 1 FROM agent_actions a
    WHERE a.id = NEW.agent_action_id AND a.user_id = NEW.user_id AND a.status = 'scheduled'
      AND NEW.not_before >= coalesce(a.undo_until, '-infinity');
    IF NOT FOUND THEN
      RAISE EXCEPTION 'two_key: outbox rows for agent actions need a scheduled action and its undo window'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER outbox_check_action BEFORE INSERT ON outbox
  FOR EACH ROW EXECUTE FUNCTION outbox_check_action();

-- Decision 31: the candidate's pause, resume and cancel record a state change, yet the API login may not insert into
-- agent_actions. This function inserts one kind of row, state_change, for the caller's own campaign and nothing else; the
-- API login still cannot write an outside action or a check.
CREATE FUNCTION record_state_change(p_campaign_id uuid, p_cause text, p_outcome_code text,
                                    p_inputs jsonb DEFAULT '{}')
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  owner_id uuid;
  run_id   uuid;
  next_seq integer;
  new_id   bigint;
BEGIN
  -- the campaign row lock also serialises two first calls, so a campaign never gets two control runs
  SELECT c.user_id INTO owner_id FROM campaigns c
  WHERE c.id = p_campaign_id AND c.user_id = app_user_id() FOR UPDATE;
  IF owner_id IS NULL THEN
    RAISE EXCEPTION 'record_state_change: not your campaign' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT r.id INTO run_id FROM agent_runs r
  WHERE r.campaign_id = p_campaign_id AND r.agent_role = 'guardian'
  ORDER BY r.started_at, r.id LIMIT 1 FOR UPDATE;
  IF run_id IS NULL THEN
    INSERT INTO agent_runs (user_id, campaign_id, agent_role, config_version)
    VALUES (owner_id, p_campaign_id, 'guardian', 'control') RETURNING id INTO run_id;
  END IF;
  SELECT coalesce(max(a.seq), 0) + 1 INTO next_seq FROM agent_actions a WHERE a.agent_run_id = run_id;
  INSERT INTO agent_actions (agent_run_id, user_id, campaign_id, seq, kind, status, summary, outcome_code, inputs)
  VALUES (run_id, owner_id, p_campaign_id, next_seq, 'state_change', 'recorded', left(p_cause, 200),
          p_outcome_code, coalesce(p_inputs, '{}'::jsonb))
  RETURNING id INTO new_id;
  RETURN new_id;
END;
$$;
REVOKE EXECUTE ON FUNCTION record_state_change(uuid, text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_state_change(uuid, text, text, jsonb) TO app_api, app_worker;

-- 5.3 Candidate isolation: the API acts for one person per transaction, the worker serves every person.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['campaigns', 'campaign_steps', 'company_dossiers', 'agent_runs', 'agent_actions', 'approvals',
                           'autonomy_settings', 'guardian_checks'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY candidate_own ON %I TO app_api
         USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)
         WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL)', t);
    EXECUTE format('CREATE POLICY worker_all ON %I TO app_worker USING (true) WITH CHECK (true)', t);
  END LOOP;
END;
$$;

-- 5.7 Privileges. The worker creates rows in the guarded tables; people move them forward; triggers check every move.
GRANT SELECT, UPDATE         ON agent_actions TO app_api;
GRANT SELECT, INSERT, UPDATE ON agent_actions TO app_worker;
GRANT SELECT                 ON guardian_checks TO app_api;
GRANT SELECT, INSERT         ON guardian_checks TO app_worker;
GRANT SELECT, INSERT, UPDATE ON approvals TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON campaigns, campaign_steps, company_dossiers, agent_runs, autonomy_settings
  TO app_api, app_worker;
