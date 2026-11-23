-- Revision p5_0501_chat_tailor_apply, part 1: the eight enum types and the seven tables of chat, tailoring and applying.
-- Read by db/versions/p5_0501_chat_tailor_apply.py, which runs it as app_owner. DATA_MODEL 2.1 (enums), 3.8 (tailor and
-- apply), 3.12 (chat) and 3.14 (the one foreign key that can exist yet).
--
-- Copied from DATA_MODEL, with the Phase 5 additions it records: chat_messages.finish_reason and the partial unique index
-- application_packets_one_current_job.
--
-- Left out on purpose: the foreign keys from application_packets.campaign_id and applications.campaign_id to campaigns
-- (DATA_MODEL 3.14, lines 1700 and 1701). The campaigns table arrives in Phase 7, which adds both constraints; until
-- then the two columns are plain nullable uuids and every row Phase 5 writes leaves them NULL.

-- A type that already exists is left as it is. In this history no earlier revision made any of them.
DO $$ BEGIN
  IF to_regtype('tailored_status') IS NULL THEN
    CREATE TYPE tailored_status AS ENUM ('generating', 'ready', 'failed');
  END IF;
  IF to_regtype('edit_verifier_status') IS NULL THEN
    CREATE TYPE edit_verifier_status AS ENUM ('passed', 'discarded');
  END IF;
  IF to_regtype('edit_discard_reason') IS NULL THEN
    CREATE TYPE edit_discard_reason AS ENUM ('new_entity', 'changed_number', 'changed_date', 'changed_title',
                                             'changed_employer', 'not_entailed', 'no_supporting_fact');
  END IF;
  IF to_regtype('edit_decision') IS NULL THEN
    CREATE TYPE edit_decision AS ENUM ('pending', 'approved', 'rejected');
  END IF;
  IF to_regtype('application_status') IS NULL THEN
    CREATE TYPE application_status AS ENUM ('applied', 'interview', 'offer', 'rejected', 'no_response');
  END IF;
  IF to_regtype('packet_status') IS NULL THEN
    CREATE TYPE packet_status AS ENUM ('draft', 'ready', 'approved', 'superseded');
  END IF;
  IF to_regtype('chat_session_kind') IS NULL THEN
    CREATE TYPE chat_session_kind AS ENUM ('onboarding', 'open');
  END IF;
  IF to_regtype('chat_role') IS NULL THEN
    CREATE TYPE chat_role AS ENUM ('user', 'assistant', 'tool');
  END IF;
END $$;

-- DATA_MODEL 3.8, tailoring.
CREATE TABLE tailored_resumes (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id              bigint          NOT NULL REFERENCES jobs(id),
  job_content_version integer         NOT NULL,
  base_document_id    uuid            NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  record_version      bigint          NOT NULL,
  status              tailored_status NOT NULL DEFAULT 'generating',
  prompt_version      text,
  discarded_count     integer         NOT NULL DEFAULT 0,   -- edits the verifier threw away (TLR-02)
  created_at          timestamptz     NOT NULL DEFAULT now()
);
CREATE INDEX tailored_resumes_user_idx ON tailored_resumes (user_id, created_at DESC);

CREATE TABLE resume_edits (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tailored_resume_id  uuid                 NOT NULL REFERENCES tailored_resumes(id) ON DELETE CASCADE,
  user_id             uuid                 NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ordinal             smallint             NOT NULL,
  section             text                 NOT NULL,
  target_fact_id      uuid                 REFERENCES profile_facts(id) ON DELETE CASCADE,  -- the fact being reworded
  before_text         text                 NOT NULL,
  after_text          text                 NOT NULL,
  supporting_fact_ids uuid[]               NOT NULL DEFAULT '{}',   -- confirmed facts that support after_text
  verifier_status     edit_verifier_status NOT NULL,
  discard_reason      edit_discard_reason,
  decision            edit_decision        NOT NULL DEFAULT 'pending',
  decided_at          timestamptz,
  created_at          timestamptz          NOT NULL DEFAULT now(),
  UNIQUE (tailored_resume_id, ordinal),
  CHECK (verifier_status = 'discarded' OR cardinality(supporting_fact_ids) >= 1),
  CHECK ((verifier_status = 'discarded') = (discard_reason IS NOT NULL)),
  CHECK (verifier_status = 'passed' OR decision = 'pending'),     -- a discarded edit can never be approved
  CHECK ((decision = 'pending') = (decided_at IS NULL))
);

-- DATA_MODEL 3.8, applying. What the candidate submits (APP-02): tailored resume, answers to common questions, a short note.
CREATE TABLE application_packets (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid          NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id             bigint        NOT NULL REFERENCES jobs(id),
  campaign_id        uuid,                                   -- no foreign key until campaigns exists (Phase 7)
  tailored_resume_id uuid          REFERENCES tailored_resumes(id) ON DELETE SET NULL,
  answers            jsonb         NOT NULL DEFAULT '[]',   -- [{"question", "answer", "fact_ids"}]
  cover_note         text          CHECK (cover_note IS NULL OR length(cover_note) <= 2000),
  status             packet_status NOT NULL DEFAULT 'draft',
  approved_at        timestamptz,
  created_at         timestamptz   NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(answers) = 'array'),
  CHECK (status NOT IN ('approved') OR approved_at IS NOT NULL)
);
CREATE UNIQUE INDEX application_packets_one_current
  ON application_packets (campaign_id) WHERE status <> 'superseded' AND campaign_id IS NOT NULL;
-- Phase 5: one current packet per job without a campaign. Campaigns arrive in Phase 7; until then a packet belongs to
-- a person and a job, and asking again supersedes the old one.
CREATE UNIQUE INDEX application_packets_one_current_job
  ON application_packets (user_id, job_id) WHERE status <> 'superseded' AND campaign_id IS NULL;

CREATE TABLE applications (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               uuid               NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  job_id                bigint             NOT NULL REFERENCES jobs(id),
  campaign_id           uuid,                                -- no foreign key until campaigns exists (Phase 7)
  application_packet_id uuid               REFERENCES application_packets(id) ON DELETE SET NULL,
  status                application_status NOT NULL,        -- latest event, kept for fast reads
  status_changed_at     timestamptz        NOT NULL,
  created_at            timestamptz        NOT NULL DEFAULT now(),
  UNIQUE (user_id, job_id),
  UNIQUE (id, user_id)
);

-- Status history (APP-01). Append-only.
CREATE TABLE application_events (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  application_id uuid               NOT NULL,
  user_id        uuid               NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status         application_status NOT NULL,
  occurred_at    timestamptz        NOT NULL,       -- when the candidate says it happened
  created_at     timestamptz        NOT NULL DEFAULT now(),
  FOREIGN KEY (application_id, user_id) REFERENCES applications (id, user_id) ON DELETE CASCADE
);
CREATE INDEX application_events_app_idx ON application_events (application_id, created_at);

-- DATA_MODEL 3.12, chat.
CREATE TABLE chat_sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid              NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind            chat_session_kind NOT NULL,
  created_at      timestamptz       NOT NULL DEFAULT now(),
  last_message_at timestamptz
);
CREATE INDEX chat_sessions_user_idx ON chat_sessions (user_id, last_message_at DESC);

CREATE TABLE chat_messages (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  uuid        NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  user_id     uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        chat_role   NOT NULL,
  content     text,                              -- user and assistant text; NULL for tool rows
  tool_name   text,                              -- tool rows: which tool ran
  refs        jsonb       NOT NULL DEFAULT '{}', -- ids the message cites: decisions, jobs, proposals (CHT-01)
  llm_call_id bigint,
  -- Phase 5: how the reply ended. NULL while it runs; only assistant rows set it.
  finish_reason text CHECK (finish_reason IN ('complete', 'length', 'stopped', 'interrupted')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((role = 'tool') = (tool_name IS NOT NULL)),
  CHECK ((role = 'tool') = (content IS NULL))
);
CREATE INDEX chat_messages_session_idx ON chat_messages (session_id, created_at);

-- DATA_MODEL 3.14. A message stands when its cost-ledger row is deleted; the reference is cleared.
ALTER TABLE chat_messages ADD FOREIGN KEY (llm_call_id) REFERENCES llm_calls(id) ON DELETE SET NULL;
