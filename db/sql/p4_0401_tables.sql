-- Revision p4_0401_match_brief, part 2: the six tables of matching and the brief. Read by db/versions/p4_0401_match_brief.py,
-- which runs it as app_owner. DATA_MODEL 3.6 (match) and 3.7 (briefs and brief_items; outbox is 0002_platform's), 3.14.
--
-- Foreign keys to tables of later phases are left out: match_runs.role_id (roles) and brief_items.approval_id,
-- agent_action_id and introduction_id (approvals, agent_actions, introductions) stay plain nullable columns, and the phase
-- that creates the target table adds the constraint (DATA_MODEL 3.14). The CHECKs on them are all here.

-- One matching pass: for a candidate (nightly, onboarding, on demand) or for an employer's role.
CREATE TABLE match_runs (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trigger                 match_trigger    NOT NULL,
  user_id                 uuid             REFERENCES users(id) ON DELETE CASCADE,  -- NULL for trigger 'role'
  role_id                 uuid,                                  -- trigger 'role' only
  local_date              date             NOT NULL,             -- in the candidate's time zone
  status                  match_run_status NOT NULL DEFAULT 'running',
  record_version          bigint,                                -- candidate runs: the record version read
  since                   timestamptz,                           -- jobs changed after this were new; NULL = whole index
  ranker_version          text             NOT NULL,
  evidence_prompt_version text,
  retrieved_count         integer          NOT NULL DEFAULT 0,
  filtered_count          integer          NOT NULL DEFAULT 0,   -- every job the constraints removed, stored or not
  shown_count             smallint         NOT NULL DEFAULT 0 CHECK (shown_count BETWEEN 0 AND 5),
  error_code              text,
  started_at              timestamptz      NOT NULL DEFAULT now(),
  finished_at             timestamptz,
  CHECK ((trigger = 'role') = (role_id IS NOT NULL AND user_id IS NULL)),
  CHECK (trigger = 'role' OR record_version IS NOT NULL)
);
-- One nightly run per candidate per local date: the first double-send guard.
CREATE UNIQUE INDEX match_runs_one_nightly ON match_runs (user_id, local_date) WHERE trigger = 'nightly';
CREATE INDEX match_runs_latest_idx ON match_runs (user_id, started_at DESC) WHERE status = 'completed';

-- The decision ledger: one row per job a run considered and recorded. Append-only.
CREATE TABLE match_decisions (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  match_run_id        uuid              NOT NULL REFERENCES match_runs(id) ON DELETE CASCADE,
  user_id             uuid              NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id              bigint            NOT NULL REFERENCES jobs(id),
  job_content_version integer           NOT NULL,
  local_date          date              NOT NULL,
  outcome             match_outcome     NOT NULL,
  reason_codes        match_reason[]    NOT NULL CHECK (cardinality(reason_codes) >= 1),
  violated_kinds      constraint_kind[] NOT NULL DEFAULT '{}',   -- 'filtered' rows: every constraint the job broke
  unverified          unverified_flag[] NOT NULL DEFAULT '{}',   -- fields the posting did not state
  retrieval_rank      integer,
  rrf_score           real,
  fit_score           real,                                      -- rules ranker; NULL if not scored
  rank_position       smallint,                                  -- 1 to 5 for 'shown' rows
  summary             text,                                      -- one sentence for 'shown' and 'introduced'
  change_note         text,                                      -- reason 'material_change'
  created_at          timestamptz       NOT NULL DEFAULT now(),
  UNIQUE (match_run_id, user_id, job_id),
  UNIQUE (id, user_id),
  CHECK ((outcome = 'shown') = (rank_position IS NOT NULL)),
  CHECK (rank_position BETWEEN 1 AND 5),
  CHECK ((outcome = 'filtered') = (cardinality(violated_kinds) > 0)),
  CHECK ((outcome = 'filtered') = ('hard_constraint' = ANY (reason_codes))),
  CHECK (outcome NOT IN ('shown', 'introduced') OR summary IS NOT NULL),
  CHECK (NOT ('material_change' = ANY (reason_codes)) OR change_note IS NOT NULL)
);
-- MAT-02: at most five shown per candidate per local date, whatever started the run.
CREATE UNIQUE INDEX match_decisions_daily_cap
  ON match_decisions (user_id, local_date, rank_position) WHERE outcome = 'shown';
-- MAT-07: a job content version is shown to a candidate at most once.
CREATE UNIQUE INDEX match_decisions_shown_once
  ON match_decisions (user_id, job_id, job_content_version) WHERE outcome = 'shown';
CREATE INDEX match_decisions_ledger_idx ON match_decisions (user_id, local_date DESC, outcome);
CREATE INDEX match_decisions_expiry_idx ON match_decisions (created_at) WHERE outcome IN ('withheld', 'filtered');

-- One verdict per decision and requirement, citing confirmed facts by id. Append-only.
CREATE TABLE match_evidence (
  id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  match_decision_id  uuid             NOT NULL REFERENCES match_decisions(id) ON DELETE CASCADE,
  user_id            uuid             NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_requirement_id bigint           NOT NULL REFERENCES job_requirements(id),
  verdict            evidence_verdict NOT NULL,
  fact_ids           uuid[]           NOT NULL DEFAULT '{}',   -- profile_facts cited; checked by trigger
  note               text             CHECK (note IS NULL OR length(note) <= 300),
  llm_call_id        bigint,
  created_at         timestamptz      NOT NULL DEFAULT now(),
  UNIQUE (match_decision_id, job_requirement_id),
  CHECK ((verdict = 'no_evidence') = (cardinality(fact_ids) = 0))
);
CREATE INDEX match_evidence_decision_idx ON match_evidence (match_decision_id);

-- Save and dismiss (MAT-08). Append-only; current state is the latest row per job.
CREATE TABLE feedback_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id            bigint        NOT NULL REFERENCES jobs(id),
  match_decision_id uuid,
  kind              feedback_kind NOT NULL,
  dismiss_reason    dismiss_reason,
  comment           text          CHECK (comment IS NULL OR length(comment) <= 500),
  created_at        timestamptz   NOT NULL DEFAULT now(),
  FOREIGN KEY (match_decision_id, user_id) REFERENCES match_decisions (id, user_id) ON DELETE CASCADE,
  CHECK ((kind = 'dismiss') = (dismiss_reason IS NOT NULL))
);
CREATE INDEX feedback_events_job_idx ON feedback_events (user_id, job_id, created_at DESC);

-- DATA_MODEL 3.14: the cost-ledger row that produced a verdict; the verdict survives the ledger row.
ALTER TABLE match_evidence ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL;

-- One brief per candidate, local date and channel (BRF-01). The unique key is the double-send guard.
CREATE TABLE briefs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  local_date   date          NOT NULL,
  channel      brief_channel NOT NULL,
  status       brief_status  NOT NULL DEFAULT 'composed',
  match_run_id uuid          REFERENCES match_runs(id) ON DELETE SET NULL,
  covers_since timestamptz,              -- "done since the last brief" starts here (BRF-02)
  send_after   timestamptz,              -- planned send time, before 07:00 local
  sent_at      timestamptz,
  created_at   timestamptz   NOT NULL DEFAULT now(),
  CONSTRAINT briefs_one_per_day UNIQUE (user_id, local_date, channel)
);

-- What a brief lists: matches, decisions waiting for the candidate, and work done since the last brief.
CREATE TABLE brief_items (
  brief_id          uuid            NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
  position          smallint        NOT NULL CHECK (position >= 1),
  user_id           uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind              brief_item_kind NOT NULL,
  match_decision_id uuid            REFERENCES match_decisions(id) ON DELETE CASCADE,
  approval_id       uuid,
  agent_action_id   bigint,
  introduction_id   uuid,
  PRIMARY KEY (brief_id, position),
  CHECK ((kind = 'match')    = (match_decision_id IS NOT NULL)),
  CHECK ((kind = 'approval') = (approval_id IS NOT NULL)),
  CHECK ((kind = 'activity') = (agent_action_id IS NOT NULL)),
  CHECK ((kind = 'introduction') = (introduction_id IS NOT NULL)),
  CHECK (num_nonnulls(match_decision_id, approval_id, agent_action_id, introduction_id) = 1)
);
