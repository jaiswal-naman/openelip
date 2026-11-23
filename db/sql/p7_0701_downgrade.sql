-- Revision p7_0701_campaigns_guardian, downgrade: back to the shape revision p56_merge_chat_hiring leaves. Read by the
-- revision, which runs it as app_owner.
--
-- It exists so the CI migration check (up, down to 0001_baseline, up) can run on a scratch database. It is lossy by
-- nature: every campaign, step, dossier, run, action, approval, check and autonomy row is dropped. Never run it against a
-- database whose rows matter. The first statement refuses the live database, which is called app.

DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

DROP TRIGGER outbox_check_action ON outbox;
DROP FUNCTION outbox_check_action();
ALTER TABLE brief_items DROP CONSTRAINT IF EXISTS brief_items_approval_id_fkey;
ALTER TABLE brief_items DROP CONSTRAINT IF EXISTS brief_items_agent_action_id_fkey;
ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_agent_action_id_fkey;
ALTER TABLE application_packets DROP CONSTRAINT IF EXISTS application_packets_campaign_id_fkey;
ALTER TABLE applications DROP CONSTRAINT IF EXISTS applications_campaign_id_fkey;
ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_campaign_id_fkey;
ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS llm_calls_agent_run_id_fkey;

DROP FUNCTION record_state_change(uuid, text, text, jsonb);
-- the tables, with their triggers, indexes, policies, grants and foreign keys
DROP TABLE guardian_checks, autonomy_settings, approvals, agent_actions, agent_runs, company_dossiers, campaign_steps,
           campaigns;
DROP FUNCTION agent_actions_guard(), approvals_decide_once(), action_always_needs_approval(action_type);
DROP TYPE guardian_check_kind, approval_status, autonomy_level, policy_decision, action_status, action_type, action_kind,
          agent_run_status, agent_role, step_status, campaign_closed_reason, campaign_stage;
