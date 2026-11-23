-- Revision p6_0601_hiring, part 2: the seven tables of the hiring desk and the foreign keys that point at them from tables
-- of earlier phases. Read by db/versions/p6_0601_hiring.py, which runs it as app_owner. DATA_MODEL 3.9 and 3.14.
--
-- Employer roles are published into the job index as unlisted jobs (job_sources.ats = 'direct', jobs.is_listed = false,
-- roles.job_id), so the hiring desk reuses requirement extraction, the constraint filter, the decision ledger and the
-- citation trigger unchanged (DATA_MODEL 11, issue 3). Not here on purpose: the Twin and agent-token tables of later
-- phases, and their foreign keys.

CREATE TABLE organizations (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text        NOT NULL,
  name_key     text        GENERATED ALWAYS AS (company_key(name)) STORED,  -- compared with candidates' blocks
  timezone     text        NOT NULL DEFAULT 'UTC',   -- business days for the answer clock are counted here
  is_synthetic boolean     NOT NULL DEFAULT false,
  created_by   uuid        REFERENCES users(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Members and pending invitations (HIR-01). An invitation has an email and no user until it is accepted.
CREATE TABLE org_members (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid          NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id         uuid          REFERENCES users(id) ON DELETE CASCADE,
  invited_email   text,
  role            org_role      NOT NULL DEFAULT 'member',
  status          member_status NOT NULL DEFAULT 'invited',
  invited_by      uuid          REFERENCES users(id) ON DELETE SET NULL,
  created_at      timestamptz   NOT NULL DEFAULT now(),
  joined_at       timestamptz,
  removed_at      timestamptz,
  CHECK (user_id IS NOT NULL OR invited_email IS NOT NULL),
  CHECK (status <> 'active' OR (user_id IS NOT NULL AND joined_at IS NOT NULL)),
  CHECK ((status = 'removed') = (removed_at IS NOT NULL))
);
CREATE UNIQUE INDEX org_members_one_per_user ON org_members (organization_id, user_id)
  WHERE user_id IS NOT NULL AND status <> 'removed';
CREATE UNIQUE INDEX org_members_one_invite ON org_members (organization_id, lower(invited_email))
  WHERE status = 'invited';
CREATE INDEX org_members_user_idx ON org_members (user_id) WHERE status = 'active';

CREATE TABLE roles (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id           uuid             NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  title                     text             NOT NULL,
  description               text             NOT NULL,
  status                    role_status      NOT NULL DEFAULT 'draft',
  country                   char(2),
  city                      text,
  work_arrangements         work_arrangement[] NOT NULL DEFAULT '{}',
  employment_type           employment_type  NOT NULL DEFAULT 'unknown',
  seniority                 seniority        NOT NULL DEFAULT 'unknown',
  salary_min                integer,
  salary_max                integer,
  salary_currency           char(3),
  visa_sponsorship          visa_sponsorship NOT NULL DEFAULT 'unknown',
  job_id                    bigint           UNIQUE REFERENCES jobs(id),  -- the unlisted index row it is published as
  requirements_confirmed_at timestamptz,
  created_by                uuid             REFERENCES users(id) ON DELETE SET NULL,
  created_at                timestamptz      NOT NULL DEFAULT now(),
  opened_at                 timestamptz,
  closed_at                 timestamptz,
  CHECK (salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max),
  -- HIR-02: a role cannot go live without confirmed requirements and a pay range.
  CONSTRAINT roles_live_needs_pay_and_requirements CHECK (
    status = 'draft'
    OR (salary_min IS NOT NULL AND salary_max IS NOT NULL AND salary_currency IS NOT NULL
        AND requirements_confirmed_at IS NOT NULL AND job_id IS NOT NULL AND opened_at IS NOT NULL)),
  -- Phase 6 addition: the target of role_requirements' composite key, so an item cannot name another organization's role.
  CONSTRAINT roles_id_organization_key UNIQUE (id, organization_id)
);
CREATE INDEX roles_org_idx ON roles (organization_id, status);

-- The employer's working list of requirements, confirmed item by item (HIR-02).
-- Phase 6 addition: DATA_MODEL has role_id REFERENCES roles(id). Row-level security only asks whether the caller belongs
-- to the row's organization_id, so a member of one organization could attach an item that names their own organization
-- to another organization's role (the worker, which reads every row, would then read it into that role). The composite
-- key below makes the item's organization the role's own, on insert and on update (the convention of DATA_MODEL 1.2).
CREATE TABLE role_requirements (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_id         uuid                   NOT NULL,
  organization_id uuid                   NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  ordinal         smallint               NOT NULL,
  importance      requirement_importance NOT NULL,
  kind            requirement_kind       NOT NULL,
  text            text                   NOT NULL,
  span            int4range,             -- in roles.description; NULL when the employer typed the item in
  is_confirmed    boolean                NOT NULL DEFAULT false,
  created_at      timestamptz            NOT NULL DEFAULT now(),
  UNIQUE (role_id, ordinal),
  FOREIGN KEY (role_id, organization_id) REFERENCES roles (id, organization_id) ON DELETE CASCADE
);

-- A private ask to one candidate for one role, and the employer's answer clock once accepted.
CREATE TABLE introductions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id        uuid                NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role_id                uuid                NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  user_id                uuid                NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- the candidate
  match_decision_id      uuid                NOT NULL REFERENCES match_decisions(id) ON DELETE CASCADE,
  status                 introduction_status NOT NULL DEFAULT 'asked',
  share_preview          jsonb               NOT NULL,  -- exactly what the employer receives on acceptance (HIR-04)
  asked_at               timestamptz         NOT NULL DEFAULT now(),
  expires_at             timestamptz         NOT NULL,
  responded_at           timestamptz,                   -- the candidate's accept or decline
  answer_due_at          timestamptz,                   -- set on acceptance: five business days later (HIR-06)
  employer_answer        employer_answer,               -- NULL while the employer owes an answer
  employer_answer_reason text,
  employer_answered_by   uuid                REFERENCES users(id) ON DELETE SET NULL,
  employer_answered_at   timestamptz,
  created_at             timestamptz         NOT NULL DEFAULT now(),
  CONSTRAINT introductions_once UNIQUE (role_id, user_id),       -- never asked twice for the same role
  CHECK (expires_at = asked_at + interval '7 days'),           -- HIR-04: silence for seven days counts as no
  CHECK ((status IN ('accepted', 'declined')) = (responded_at IS NOT NULL)),
  CHECK ((status = 'accepted') = (answer_due_at IS NOT NULL)),
  CHECK ((employer_answer IS NULL) = (employer_answered_at IS NULL)),
  CHECK (employer_answer IS NULL
         OR (status = 'accepted' AND length(trim(coalesce(employer_answer_reason, ''))) > 0))
);
CREATE INDEX introductions_candidate_idx ON introductions (user_id, asked_at DESC);
CREATE INDEX introductions_expiry_idx ON introductions (expires_at) WHERE status = 'asked';
-- The answer clock: what each organization owes.
CREATE INDEX introductions_owed_idx ON introductions (organization_id, answer_due_at)
  WHERE status = 'accepted' AND employer_answer IS NULL;

-- The employer's view of a candidate who said yes: a frozen copy of what the candidate agreed to share.
CREATE TABLE shortlist_entries (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id     uuid        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role_id             uuid        NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  introduction_id     uuid        NOT NULL UNIQUE REFERENCES introductions(id) ON DELETE CASCADE,
  user_id             uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  shared_payload      jsonb       NOT NULL,   -- equals introductions.share_preview, checked on insert
  contact_released_at timestamptz,            -- the candidate chose to share contact details
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX shortlist_entries_role_idx ON shortlist_entries (role_id, created_at DESC);

-- Scarce "serious interest" signals (HIR-08). Slots make the weekly limit a unique key.
CREATE TABLE interest_signals (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sender_side     signal_side NOT NULL,
  user_id         uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,          -- the candidate
  organization_id uuid        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role_id         uuid        NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  introduction_id uuid        REFERENCES introductions(id) ON DELETE CASCADE,  -- organization signals ride on one
  sent_by         uuid        REFERENCES users(id) ON DELETE SET NULL,
  week_start      date        NOT NULL,       -- Monday of the UTC week it was sent
  slot            smallint    NOT NULL CHECK (slot BETWEEN 1 AND 3),   -- assumption: 3 per side per week
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT interest_signals_once UNIQUE (sender_side, user_id, role_id),
  CHECK ((sender_side = 'organization') = (introduction_id IS NOT NULL)),
  CHECK (week_start = date_trunc('week', created_at AT TIME ZONE 'UTC')::date)
);
CREATE UNIQUE INDEX interest_signals_candidate_week ON interest_signals (user_id, week_start, slot)
  WHERE sender_side = 'candidate';
CREATE UNIQUE INDEX interest_signals_organization_week ON interest_signals (organization_id, week_start, slot)
  WHERE sender_side = 'organization';

-- DATA_MODEL 3.14. The foreign keys that needed these tables, from tables that already existed: the columns were plain
-- until now (the consent, audit, job source, match run and brief item rows of the earlier phases). The other keys of
-- that section point at tables of later phases. A key that is already there is left alone.
DO $$
DECLARE
  fk record;
BEGIN
  FOR fk IN SELECT * FROM (VALUES
      ('consents',    'consents_organization_id_fkey',    'FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE'),
      ('consents',    'consents_role_id_fkey',            'FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE'),
      ('audit_log',   'audit_log_organization_id_fkey',   'FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE SET NULL'),
      ('job_sources', 'job_sources_organization_id_fkey', 'FOREIGN KEY (organization_id) REFERENCES organizations(id)'),
      ('match_runs',  'match_runs_role_id_fkey',          'FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE'),
      ('brief_items', 'brief_items_introduction_id_fkey', 'FOREIGN KEY (introduction_id) REFERENCES introductions(id) ON DELETE CASCADE')
    ) AS keys(table_name, constraint_name, definition)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint c
                   WHERE c.conrelid = fk.table_name::regclass AND c.conname = fk.constraint_name) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', fk.table_name, fk.constraint_name, fk.definition);
    END IF;
  END LOOP;
END $$;
