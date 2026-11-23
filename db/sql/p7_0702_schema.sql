-- Revision p7_0702_langgraph, part 1: the schema that holds the LangGraph checkpoints (DATA_MODEL 3.15, first statements).
-- Read by the revision, which runs it as app_owner on an autocommit connection so the schema is committed before the
-- library creates its tables in it.
CREATE SCHEMA IF NOT EXISTS langgraph AUTHORIZATION app_owner;
REVOKE ALL ON SCHEMA langgraph FROM PUBLIC;
GRANT USAGE ON SCHEMA langgraph TO app_worker;                 -- no CREATE: runtime roles never call setup()
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA langgraph
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_worker;
-- Row-level security stays off in this schema: only app_worker can reach it, and thread ids hold UUIDs only.
