-- Revision p7_0702_langgraph, part 3: erasure (DATA_MODEL 3.15, rule 11). A thread's checkpoints go in the same
-- transaction as the campaign the thread belongs to. Not here: the Twin trigger (twin_messages arrives in Phase 9).
-- Read by the revision, which runs it as app_owner.
CREATE FUNCTION langgraph_delete_thread(p_thread_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  DELETE FROM langgraph.checkpoint_writes WHERE thread_id = p_thread_id;
  DELETE FROM langgraph.checkpoint_blobs  WHERE thread_id = p_thread_id;
  DELETE FROM langgraph.checkpoints       WHERE thread_id = p_thread_id;
END;
$$;
REVOKE EXECUTE ON FUNCTION langgraph_delete_thread(text) FROM PUBLIC;

CREATE FUNCTION campaigns_delete_thread() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM langgraph_delete_thread('campaign:' || OLD.id);
  RETURN NULL;
END;
$$;
REVOKE EXECUTE ON FUNCTION campaigns_delete_thread() FROM PUBLIC;
CREATE TRIGGER campaigns_delete_thread AFTER DELETE ON campaigns
  FOR EACH ROW EXECUTE FUNCTION campaigns_delete_thread();
