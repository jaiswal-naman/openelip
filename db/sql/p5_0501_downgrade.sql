-- Revision p5_0501_chat_tailor_apply, downgrade: back to the shape revision p4_0401_match_brief leaves. Read by the revision,
-- which runs it as app_owner.
--
-- It exists so the CI migration check (up, down to 0001_baseline, up) can run on a scratch database. It is lossy by
-- nature: every chat, tailored resume, packet and application is dropped. Never run it against a database whose rows
-- matter. This revision changed no function or table that stays; it added one grant on two tables that stay, revoked
-- below.
--
-- The first statement refuses the live database, which is called app. The migration check in CI and the tests use other
-- names (migration_check, names ending in _test).

DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

REVOKE INSERT ON llm_calls, model_registry FROM app_api;

-- The tables, with their triggers, indexes, policies, grants and foreign keys, then the trigger functions that served
-- them, then the types, which nothing uses any more.
DROP TABLE application_events, applications, application_packets, resume_edits, tailored_resumes,
           chat_messages, chat_sessions;
DROP FUNCTION resume_edits_decide_once(), resume_edits_check_support();
DROP TYPE chat_role, chat_session_kind, packet_status, application_status, edit_decision, edit_discard_reason,
          edit_verifier_status, tailored_status;
