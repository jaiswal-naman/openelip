-- Revision p7_0701_campaigns_guardian, part 1: the campaign and control enums and the eight tables. Read by the revision,
-- which runs it as app_owner. DATA_MODEL enums (campaigns and control) and 3.10 (campaigns and guardian), 3.14 (the foreign
-- keys of the Phase 7 columns), 3.13 (llm_calls).
--
-- Differences from the document (each is a line in DATA_MODEL 3.10 too):
--   campaigns.follow_up_email         new: the one address the candidate typed for follow-ups (decision 5)
--   agent_actions undo_until CHECK    relaxed: it is set once, when a follow-up is scheduled (decision 3)
-- Left out on purpose: the Twin foreign keys (agent_runs.twin_session_id stays a plain column until Phase 9).

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'campaign_stage') THEN
    CREATE TYPE campaign_stage AS ENUM ('researching', 'needs_you', 'applied', 'talking', 'interview', 'offer', 'closed');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'campaign_closed_reason') THEN
    CREATE TYPE campaign_closed_reason AS ENUM ('cancelled', 'rejected', 'no_response', 'job_closed',
                                                'offer_accepted', 'offer_declined');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'step_status') THEN
    CREATE TYPE step_status AS ENUM ('pending', 'running', 'waiting', 'done', 'failed', 'skipped', 'cancelled');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'agent_role') THEN
    CREATE TYPE agent_role AS ENUM ('scout', 'researcher', 'analyst', 'writer', 'operator', 'chaser', 'coach', 'twin',
                                    'guardian');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'agent_run_status') THEN
    CREATE TYPE agent_run_status AS ENUM ('running', 'succeeded', 'failed', 'cancelled');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'action_kind') THEN
    CREATE TYPE action_kind AS ENUM ('model_call', 'tool_call', 'state_change', 'outside_action');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'action_type') THEN
    CREATE TYPE action_type AS ENUM ('submit_application', 'send_follow_up', 'twin_answer');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'action_status') THEN
    CREATE TYPE action_status AS ENUM ('recorded', 'proposed', 'awaiting_approval', 'scheduled', 'executed', 'blocked',
                                       'rejected', 'expired', 'cancelled', 'failed');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'policy_decision') THEN
    CREATE TYPE policy_decision AS ENUM ('allow', 'ask', 'deny');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'autonomy_level') THEN
    CREATE TYPE autonomy_level AS ENUM ('ask_first', 'tell_me_after', 'never');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'approval_status') THEN
    CREATE TYPE approval_status AS ENUM ('pending', 'approved', 'rejected', 'expired');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'guardian_check_kind') THEN
    CREATE TYPE guardian_check_kind AS ENUM ('citations', 'consent', 'scope', 'budget', 'injection', 'rate_limit');
  END IF;
END $$;

-- One job the candidate approved, worked by the agent crew until it closes (CMP-01).
CREATE TABLE campaigns (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid           NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id               bigint         NOT NULL REFERENCES jobs(id),
  match_decision_id    uuid           REFERENCES match_decisions(id) ON DELETE SET NULL,  -- the approved match
  introduction_id      uuid           REFERENCES introductions(id) ON DELETE SET NULL,    -- or the accepted introduction
  stage                campaign_stage NOT NULL DEFAULT 'researching',
  paused_at            timestamptz,                       -- CMP-07; NULL = running
  closed_reason        campaign_closed_reason,
  closed_at            timestamptz,
  budget_microusd      bigint         NOT NULL CHECK (budget_microusd > 0),   -- model spend the Guardian allows
  follow_up_after_days smallint       NOT NULL DEFAULT 7 CHECK (follow_up_after_days BETWEEN 1 AND 60),  -- assumption
  follow_up_email      text           CHECK (follow_up_email IS NULL OR (length(follow_up_email) <= 254
                                          AND follow_up_email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$')),
  last_update_at       timestamptz    NOT NULL DEFAULT now(),  -- last news from outside; drives the Chaser
  lock_version         integer        NOT NULL DEFAULT 0,      -- optimistic lock; only campaigns.stages writes stage
  created_at           timestamptz    NOT NULL DEFAULT now(),
  updated_at           timestamptz    NOT NULL DEFAULT now(),
  UNIQUE (id, user_id),
  CHECK ((stage = 'closed') = (closed_at IS NOT NULL AND closed_reason IS NOT NULL)),
  CHECK (paused_at IS NULL OR stage <> 'closed')
);
CREATE UNIQUE INDEX campaigns_one_open_per_job ON campaigns (user_id, job_id) WHERE stage <> 'closed';
CREATE INDEX campaigns_board_idx ON campaigns (user_id, stage, updated_at DESC);
CREATE TRIGGER campaigns_touch BEFORE UPDATE ON campaigns FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- The durable plan: one row per unit of work, keyed so a restart can never duplicate it (CMP-06).
CREATE TABLE campaign_steps (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id             uuid        NOT NULL,
  user_id                 uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  step_key                text        NOT NULL,     -- research, fit, packet_draft, packet, follow_up (numbered), interview_prep
  agent_role              agent_role  NOT NULL,
  status                  step_status NOT NULL DEFAULT 'pending',
  due_at                  timestamptz NOT NULL DEFAULT now(),   -- not before this time: timers that wait for days
  attempts                smallint    NOT NULL DEFAULT 0,
  waiting_for_approval_id uuid,
  output                  jsonb,                    -- structured result, for example interview preparation (CMP-05)
  error_code              text,
  created_at              timestamptz NOT NULL DEFAULT now(),
  started_at              timestamptz,
  finished_at             timestamptz,
  FOREIGN KEY (campaign_id, user_id) REFERENCES campaigns (id, user_id) ON DELETE CASCADE,
  CONSTRAINT campaign_steps_once UNIQUE (campaign_id, step_key)
);
CREATE INDEX campaign_steps_due_idx ON campaign_steps (due_at) WHERE status = 'pending';

-- The Researcher's cited one-page company dossier (CMP-02).
CREATE TABLE company_dossiers (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id  uuid        NOT NULL,
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  company_name text        NOT NULL,
  version      smallint    NOT NULL DEFAULT 1,
  claims       jsonb       NOT NULL,   -- a list of {text, sources: [{url, quote, retrieved_at}]}
  agent_run_id uuid,
  created_at   timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (campaign_id, user_id) REFERENCES campaigns (id, user_id) ON DELETE CASCADE,
  UNIQUE (campaign_id, version),
  CHECK (jsonb_typeof(claims) = 'array'),
  -- every claim carries at least one source with a URL
  CHECK (NOT jsonb_path_exists(claims, '$[*] ? (!(exists(@.sources[0].url)))'))
);

-- One execution of one crew role.
CREATE TABLE agent_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid             NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- whose work this is
  campaign_id      uuid             REFERENCES campaigns(id) ON DELETE CASCADE,
  campaign_step_id uuid             REFERENCES campaign_steps(id) ON DELETE CASCADE,
  twin_session_id  uuid,                        -- no foreign key until twin_sessions exists (Phase 9)
  agent_role       agent_role       NOT NULL,
  status           agent_run_status NOT NULL DEFAULT 'running',
  config_version   text             NOT NULL,   -- prompt and tool configuration, for replay
  error_code       text,
  started_at       timestamptz      NOT NULL DEFAULT now(),
  finished_at      timestamptz
);
CREATE INDEX agent_runs_campaign_idx ON agent_runs (campaign_id, agent_role, started_at DESC);

-- The flight recorder (GRD-03): every step an agent takes, in order. Content is immutable;
-- only status, executed_at, outcome_code and (once, when a follow-up is scheduled) undo_until move forward, under the
-- trigger in the next file.
CREATE TABLE agent_actions (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_run_id    uuid            NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  user_id         uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  campaign_id     uuid            REFERENCES campaigns(id) ON DELETE CASCADE,
  seq             integer         NOT NULL,           -- order within the run
  kind            action_kind     NOT NULL,
  action_type     action_type,                        -- outside actions only
  status          action_status   NOT NULL,
  summary         text            NOT NULL,           -- one line for the timeline
  inputs          jsonb           NOT NULL DEFAULT '{}',  -- ids of what the step read; never raw resume or chat text
  evidence        jsonb           NOT NULL DEFAULT '[]',  -- cited fact, requirement and dossier references
  payload         jsonb,                              -- outside actions: exactly what would leave the system
  organization_id uuid            REFERENCES organizations(id) ON DELETE SET NULL,  -- who it reaches, if anyone
  llm_call_id     bigint,
  policy_decision policy_decision,                    -- outside actions: the policy engine's verdict, at insert
  policy_version  text,
  undo_until      timestamptz,                        -- outgoing messages: earliest send time (GRD-04)
  outcome_code    text,                               -- why it was blocked, failed or cancelled
  created_at      timestamptz     NOT NULL DEFAULT now(),
  executed_at     timestamptz,
  UNIQUE (agent_run_id, seq),
  UNIQUE (id, user_id),
  CHECK ((kind = 'outside_action') = (action_type IS NOT NULL)),
  CHECK ((kind = 'outside_action') = (policy_decision IS NOT NULL AND policy_version IS NOT NULL
                                      AND payload IS NOT NULL)),
  CHECK ((kind = 'outside_action') = (status <> 'recorded')),
  CHECK ((status = 'executed') = (executed_at IS NOT NULL)),
  CHECK (status NOT IN ('blocked', 'failed') OR outcome_code IS NOT NULL),
  -- GRD-04, amended (decision 3): undo_until is set when the follow-up is scheduled, not when it is proposed
  CHECK (action_type IS DISTINCT FROM 'send_follow_up' OR status NOT IN ('scheduled', 'executed')
         OR undo_until IS NOT NULL),
  CHECK (policy_decision IS DISTINCT FROM 'deny' OR status IN ('proposed', 'blocked'))
);
CREATE INDEX agent_actions_campaign_idx ON agent_actions (campaign_id, created_at);
CREATE INDEX agent_actions_open_idx ON agent_actions (user_id, status)
  WHERE status IN ('proposed', 'awaiting_approval', 'scheduled');

-- The candidate's yes or no on one proposed action (BRF-02, CMP-03, CMP-04).
CREATE TABLE approvals (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  agent_action_id bigint          NOT NULL,
  status          approval_status NOT NULL DEFAULT 'pending',
  requested_at    timestamptz     NOT NULL DEFAULT now(),
  expires_at      timestamptz     NOT NULL,
  decided_at      timestamptz,
  decided_via     text            CHECK (decided_via = 'app'),   -- never an outside agent, never an email link
  note            text,
  -- Only the person the action belongs to can approve it.
  FOREIGN KEY (agent_action_id, user_id) REFERENCES agent_actions (id, user_id) ON DELETE CASCADE,
  CONSTRAINT approvals_one_per_action UNIQUE (agent_action_id),
  CHECK ((status = 'pending') = (decided_at IS NULL)),
  CHECK ((status IN ('approved', 'rejected')) = (decided_via IS NOT NULL))
);
CREATE INDEX approvals_pending_idx ON approvals (user_id, requested_at) WHERE status = 'pending';

-- The autonomy dial (GRD-01). No row means 'ask_first'.
CREATE TABLE autonomy_settings (
  user_id     uuid           NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  action_type action_type    NOT NULL,
  level       autonomy_level NOT NULL,
  updated_at  timestamptz    NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, action_type)
);

-- The Guardian's verdicts on one outside action. Append-only.
CREATE TABLE guardian_checks (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_action_id  bigint              NOT NULL,
  user_id          uuid                NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  check_kind       guardian_check_kind NOT NULL,
  passed           boolean             NOT NULL,
  reason_code      text,
  details          jsonb               NOT NULL DEFAULT '{}',
  guardian_version text                NOT NULL,
  checked_at       timestamptz         NOT NULL DEFAULT now(),
  FOREIGN KEY (agent_action_id, user_id) REFERENCES agent_actions (id, user_id) ON DELETE CASCADE,
  CHECK (passed OR reason_code IS NOT NULL)
);
CREATE INDEX guardian_checks_action_idx ON guardian_checks (agent_action_id);

-- DATA_MODEL 3.14, the foreign keys of columns that earlier phases left without one. Each is added only when it is
-- missing, so a later phase that adds one first does not make this revision fail.
DO $$
DECLARE
  fk record;
BEGIN
  FOR fk IN SELECT * FROM (VALUES
      ('brief_items', 'brief_items_approval_id_fkey',
       'FOREIGN KEY (approval_id) REFERENCES approvals(id) ON DELETE CASCADE'),
      ('brief_items', 'brief_items_agent_action_id_fkey',
       'FOREIGN KEY (agent_action_id) REFERENCES agent_actions(id) ON DELETE CASCADE'),
      ('outbox', 'outbox_agent_action_id_fkey',
       'FOREIGN KEY (agent_action_id) REFERENCES agent_actions(id) ON DELETE CASCADE'),
      ('application_packets', 'application_packets_campaign_id_fkey',
       'FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE'),
      ('applications', 'applications_campaign_id_fkey',
       'FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL'),
      ('campaign_steps', 'campaign_steps_waiting_for_approval_id_fkey',
       'FOREIGN KEY (waiting_for_approval_id) REFERENCES approvals(id) ON DELETE SET NULL'),
      ('company_dossiers', 'company_dossiers_agent_run_id_fkey',
       'FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL'),
      ('agent_actions', 'agent_actions_llm_call_id_fkey',
       'FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL'),
      ('llm_calls', 'llm_calls_campaign_id_fkey',
       'FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL'),
      ('llm_calls', 'llm_calls_agent_run_id_fkey',
       'FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL')
    ) AS v(tbl, name, ddl)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = fk.name AND conrelid = fk.tbl::regclass) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', fk.tbl, fk.name, fk.ddl);
    END IF;
  END LOOP;
END $$;
