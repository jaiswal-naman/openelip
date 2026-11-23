-- Revision p6_0601_hiring, part 1: the hiring enum types, the new eval suite value and the business-day helper. Read by
-- db/versions/p6_0601_hiring.py, which runs it as app_owner. DATA_MODEL 3.1 and 3.2.
--
-- A type that already exists is left as it is, and a function is created or replaced, so a database that got one of them
-- by hand still upgrades. In this history no earlier revision made any of them. The enums the hiring tables share with
-- the job index (work_arrangement, employment_type, seniority, visa_sponsorship, requirement_importance and
-- requirement_kind) and consent_purpose come from earlier revisions.

-- DATA_MODEL 3.1, hiring.
DO $$ BEGIN
  IF to_regtype('org_role') IS NULL THEN
    CREATE TYPE org_role AS ENUM ('owner', 'admin', 'member');
  END IF;
  IF to_regtype('member_status') IS NULL THEN
    CREATE TYPE member_status AS ENUM ('invited', 'active', 'removed');
  END IF;
  IF to_regtype('role_status') IS NULL THEN
    CREATE TYPE role_status AS ENUM ('draft', 'open', 'closed');
  END IF;
  IF to_regtype('introduction_status') IS NULL THEN
    CREATE TYPE introduction_status AS ENUM ('asked', 'accepted', 'declined', 'expired', 'withdrawn');
  END IF;
  IF to_regtype('employer_answer') IS NULL THEN
    CREATE TYPE employer_answer AS ENUM ('yes', 'no');
  END IF;
  IF to_regtype('signal_side') IS NULL THEN
    CREATE TYPE signal_side AS ENUM ('candidate', 'organization');
  END IF;
END $$;

-- Phase 6 addition (index deviation 1, noted in DATA_MODEL 1.3): the employer's role description is read into
-- requirements, and that reading has a gold set of its own. PostgreSQL cannot remove an enum value, so the downgrade
-- leaves this one.
ALTER TYPE eval_suite ADD VALUE IF NOT EXISTS 'role_intake';

-- DATA_MODEL 3.2. Five business days means Monday to Friday in the given time zone. No holiday calendar (assumption).
CREATE OR REPLACE FUNCTION add_business_days(p_start timestamptz, p_days integer, p_timezone text) RETURNS timestamptz
LANGUAGE plpgsql STABLE AS $$
DECLARE
  local_ts timestamp := p_start AT TIME ZONE p_timezone;
  added    integer   := 0;
BEGIN
  WHILE added < p_days LOOP
    local_ts := local_ts + interval '1 day';
    IF extract(isodow FROM local_ts) < 6 THEN
      added := added + 1;
    END IF;
  END LOOP;
  RETURN local_ts AT TIME ZONE p_timezone;
END;
$$;
