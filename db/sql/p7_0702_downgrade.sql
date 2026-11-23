-- Revision p7_0702_langgraph, downgrade: drops the deletion trigger and functions and the whole schema, checkpoints
-- included. Lossy; refuses the live database, which is called app. Read by the revision, which runs it as app_owner.
DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

DROP TRIGGER campaigns_delete_thread ON campaigns;
DROP FUNCTION campaigns_delete_thread();
DROP FUNCTION langgraph_delete_thread(text);
DROP SCHEMA langgraph CASCADE;
