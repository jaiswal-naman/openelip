"""Wind tunnel: scenarios, attempts, results and the five-of-five verdict view (08-01; EVL-03, EVL-04).

The DDL is copied from docs/DATA_MODEL.md (enums 2.1, tables and view 3.13, triggers 4.5, grants 5.7). Three things
differ from the document, each recorded in DATA_MODEL: row-level security is enabled on the three tables (every table
has a policy: `reference_read` for the API and match logins, `worker_all` for the worker), the view is created
`security_invoker` so the caller's grants and policies apply, and `sim_runs.candidate_user_id` is registered as
set-null.

Additive. downgrade() exists for the CI migration check on a scratch database; its first statement refuses to run on
the database called app.
"""

from alembic import op

revision = "p8_0801_wind_tunnel"
down_revision = "p7_0702_langgraph"
branch_labels = None
depends_on = None

TYPES_SQL = """
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sim_persona') THEN
    CREATE TYPE sim_persona AS ENUM ('silent', 'lowball', 'scam', 'hidden_instruction', 'cooperative');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sim_run_status') THEN
    CREATE TYPE sim_run_status AS ENUM ('running', 'passed', 'failed', 'error');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'sim_result_kind') THEN
    CREATE TYPE sim_result_kind AS ENUM ('outcome', 'violation');
  END IF;
END $$;
"""

TABLES_SQL = """
CREATE TABLE sim_scenarios (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key        text        NOT NULL,
  version    integer     NOT NULL,
  persona    sim_persona NOT NULL,
  title      text        NOT NULL,
  definition jsonb       NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (key, version)
);

CREATE TABLE sim_runs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  eval_run_id       uuid           NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  scenario_id       uuid           NOT NULL REFERENCES sim_scenarios(id),
  attempt           smallint       NOT NULL CHECK (attempt BETWEEN 1 AND 5),
  seed              bigint         NOT NULL,
  status            sim_run_status NOT NULL DEFAULT 'running',
  candidate_user_id uuid           REFERENCES users(id) ON DELETE SET NULL,
  campaign_id       uuid           REFERENCES campaigns(id) ON DELETE SET NULL,
  started_at        timestamptz    NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  UNIQUE (eval_run_id, scenario_id, attempt)
);

CREATE TABLE sim_results (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sim_run_id uuid            NOT NULL REFERENCES sim_runs(id) ON DELETE CASCADE,
  kind       sim_result_kind NOT NULL,
  code       text            NOT NULL,
  passed     boolean         NOT NULL,
  details    jsonb           NOT NULL DEFAULT '{}',
  created_at timestamptz     NOT NULL DEFAULT now()
);
CREATE INDEX sim_results_run_idx ON sim_results (sim_run_id);

-- EVL-04: a scenario passes in an eval run only if all five attempts finished and passed.
CREATE VIEW sim_scenario_verdicts WITH (security_invoker = true) AS
SELECT r.eval_run_id,
       r.scenario_id,
       count(*)                                    AS attempts,
       count(*) FILTER (WHERE r.status = 'passed') AS attempts_passed,
       count(*) = 5 AND bool_and(r.status = 'passed') AS passed
FROM sim_runs r
GROUP BY r.eval_run_id, r.scenario_id;
"""

SECURITY_SQL = """
CREATE TRIGGER sim_scenarios_append_only BEFORE UPDATE OR DELETE ON sim_scenarios
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER sim_results_append_only BEFORE UPDATE OR DELETE ON sim_results
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

ALTER TABLE sim_scenarios ENABLE ROW LEVEL SECURITY;
ALTER TABLE sim_runs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE sim_results   ENABLE ROW LEVEL SECURITY;
CREATE POLICY reference_read ON sim_scenarios FOR SELECT TO app_api, app_match USING (true);
CREATE POLICY reference_read ON sim_runs      FOR SELECT TO app_api, app_match USING (true);
CREATE POLICY reference_read ON sim_results   FOR SELECT TO app_api, app_match USING (true);
CREATE POLICY worker_all ON sim_scenarios TO app_worker USING (true) WITH CHECK (true);
CREATE POLICY worker_all ON sim_runs      TO app_worker USING (true) WITH CHECK (true);
CREATE POLICY worker_all ON sim_results   TO app_worker USING (true) WITH CHECK (true);

GRANT SELECT ON sim_scenarios, sim_runs, sim_results, sim_scenario_verdicts TO app_api, app_match;
GRANT SELECT, INSERT ON sim_scenarios, sim_results TO app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON sim_runs TO app_worker;
GRANT SELECT ON sim_scenario_verdicts TO app_worker;
"""

DOWNGRADE_SQL = """
DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;
DROP VIEW sim_scenario_verdicts;
DROP TABLE sim_results;
DROP TABLE sim_runs;
DROP TABLE sim_scenarios;
DROP TYPE sim_result_kind;
DROP TYPE sim_run_status;
DROP TYPE sim_persona;
"""


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for sql in (TYPES_SQL, TABLES_SQL, SECURITY_SQL):
        op.execute(sql)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
