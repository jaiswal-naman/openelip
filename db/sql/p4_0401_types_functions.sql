-- Revision p4_0401_match_brief, part 1: the matching and delivery enum types, the company-name helpers and the constraint
-- filter. Read by db/versions/p4_0401_match_brief.py, which runs it as app_owner. DATA_MODEL 3.1, 3.2 and 4.1.
--
-- A type that already exists is left as it is, and a function is created or replaced, so a database that got one of them
-- by hand still upgrades. In this history no earlier revision made any of them. constraint_kind, work_arrangement,
-- employment_type and seniority come from the job index and the career record; outbox_status from 0002_platform.

-- DATA_MODEL 3.1, matching and delivery.
DO $$ BEGIN
  IF to_regtype('match_trigger') IS NULL THEN
    CREATE TYPE match_trigger AS ENUM ('nightly', 'onboarding', 'on_demand', 'role');
  END IF;
  IF to_regtype('match_run_status') IS NULL THEN
    CREATE TYPE match_run_status AS ENUM ('running', 'completed', 'failed');
  END IF;
  IF to_regtype('match_outcome') IS NULL THEN
    CREATE TYPE match_outcome AS ENUM ('shown', 'introduced', 'withheld', 'filtered');
  END IF;
  IF to_regtype('match_reason') IS NULL THEN
    CREATE TYPE match_reason AS ENUM ('top_ranked', 'material_change', 'selected_for_role',
                                      'weak_fit', 'required_not_met', 'below_daily_cap',
                                      'citation_unresolved', 'already_shown', 'job_closed',
                                      'liveness_unverified', 'not_scored', 'not_retrieved',
                                      'hard_constraint', 'injection_suspected');
  END IF;
  IF to_regtype('unverified_flag') IS NULL THEN
    CREATE TYPE unverified_flag AS ENUM ('location_not_stated', 'arrangement_not_stated',
                                         'arrangement_ambiguous', 'salary_not_stated',
                                         'salary_currency_differs', 'employment_type_not_stated',
                                         'seniority_not_stated');
  END IF;
  IF to_regtype('evidence_verdict') IS NULL THEN
    CREATE TYPE evidence_verdict AS ENUM ('met', 'partly_met', 'not_met', 'no_evidence');
  END IF;
  IF to_regtype('feedback_kind') IS NULL THEN
    CREATE TYPE feedback_kind AS ENUM ('save', 'unsave', 'dismiss');
  END IF;
  IF to_regtype('dismiss_reason') IS NULL THEN
    CREATE TYPE dismiss_reason AS ENUM ('not_interested_role', 'not_interested_company', 'seniority_mismatch',
                                        'compensation', 'location', 'skills_mismatch', 'already_applied',
                                        'explanation_wrong', 'duplicate', 'job_closed', 'other');
  END IF;
  IF to_regtype('brief_channel') IS NULL THEN
    CREATE TYPE brief_channel AS ENUM ('email', 'in_app');
  END IF;
  IF to_regtype('brief_status') IS NULL THEN
    CREATE TYPE brief_status AS ENUM ('composed', 'suppressed', 'queued', 'sent', 'failed');
  END IF;
  IF to_regtype('brief_item_kind') IS NULL THEN
    CREATE TYPE brief_item_kind AS ENUM ('match', 'approval', 'activity', 'introduction');
  END IF;
END $$;

-- DATA_MODEL 3.2. A company name reduced to a comparison key: lower case, common legal suffixes removed, letters and
-- digits only. Used wherever two company names must be compared (excluded companies, employer blocks).
CREATE OR REPLACE FUNCTION company_key(p_name text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT regexp_replace(
           regexp_replace(lower(coalesce(p_name, '')),
             '\m(incorporated|inc|llc|llp|ltd|limited|corp|corporation|co|company|gmbh|plc|pvt|private)\M', '', 'g'),
           '[^a-z0-9]+', '', 'g')
$$;

CREATE OR REPLACE FUNCTION company_keys(p_names text[]) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT array_agg(DISTINCT company_key(n)) FROM unnest(p_names) AS n
$$;

-- DATA_MODEL 4.1, invariant 1. The constraint filter: one function, called by every path that selects jobs
-- (nightly, onboarding, on demand, chat search, "why not", role runs, introductions).

-- A candidate's active hard constraints, flattened. NULL in a field means "no constraint of that kind".
DO $$ BEGIN
  IF to_regtype('compiled_constraints') IS NULL THEN
    CREATE TYPE compiled_constraints AS (
      countries             char(2)[],
      cities                text[],
      arrangements          work_arrangement[],
      authorized_countries  char(2)[],
      salary_floor          integer,
      salary_currency       char(3),
      employment_types      employment_type[],
      seniorities           seniority[],
      excluded_company_keys text[]
    );
  END IF;
END $$;

CREATE OR REPLACE FUNCTION compile_constraints(p_user_id uuid) RETURNS compiled_constraints
LANGUAGE sql STABLE AS $$
  SELECT ROW(
    max(c.country_codes)    FILTER (WHERE c.kind = 'location'),
    max(c.cities)           FILTER (WHERE c.kind = 'location'),
    max(c.arrangements)     FILTER (WHERE c.kind = 'remote_policy'),
    max(c.country_codes)    FILTER (WHERE c.kind = 'work_authorization'),
    max(c.salary_floor)     FILTER (WHERE c.kind = 'salary_floor'),
    max(c.salary_currency)  FILTER (WHERE c.kind = 'salary_floor'),
    max(c.employment_types) FILTER (WHERE c.kind = 'employment_type'),
    max(c.seniorities)      FILTER (WHERE c.kind = 'seniority'),
    company_keys(max(c.company_names) FILTER (WHERE c.kind = 'excluded_company'))
  )::compiled_constraints
  FROM constraints c
  WHERE c.user_id = p_user_id AND c.superseded_at IS NULL   -- at most one active row per kind
$$;

-- Which hard constraints a job breaks. Empty array = eligible.
-- Rule for unknown values: a constraint removes a job only when a stated field contradicts it.
-- A field the posting leaves unstated passes here and is reported by unverified_fields.
CREATE OR REPLACE FUNCTION constraint_violations(j jobs, c compiled_constraints) RETURNS constraint_kind[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT array_remove(ARRAY[
    CASE WHEN (c.countries IS NOT NULL AND j.country IS NOT NULL AND j.country <> ALL (c.countries))
           OR (c.cities IS NOT NULL AND j.city IS NOT NULL
               AND NOT ('remote' = ANY (j.work_arrangements))
               AND lower(j.city) <> ALL (c.cities))
         THEN 'location'::constraint_kind END,
    CASE WHEN c.arrangements IS NOT NULL AND cardinality(j.work_arrangements) > 0
              AND NOT (j.work_arrangements && c.arrangements)
         THEN 'remote_policy'::constraint_kind END,
    CASE WHEN c.authorized_countries IS NOT NULL AND j.country IS NOT NULL
              AND j.country <> ALL (c.authorized_countries)
              AND j.visa_sponsorship <> 'offered'
         THEN 'work_authorization'::constraint_kind END,
    CASE WHEN c.salary_floor IS NOT NULL AND j.salary_max IS NOT NULL
              AND j.salary_currency = c.salary_currency AND j.salary_max < c.salary_floor
         THEN 'salary_floor'::constraint_kind END,
    CASE WHEN c.employment_types IS NOT NULL AND j.employment_type <> 'unknown'
              AND j.employment_type <> ALL (c.employment_types)
         THEN 'employment_type'::constraint_kind END,
    CASE WHEN c.seniorities IS NOT NULL AND j.seniority <> 'unknown'
              AND j.seniority <> ALL (c.seniorities)
         THEN 'seniority'::constraint_kind END,
    CASE WHEN c.excluded_company_keys IS NOT NULL
              AND company_key(j.company_name) = ANY (c.excluded_company_keys)
         THEN 'excluded_company'::constraint_kind END
  ], NULL)
$$;

-- Which constrained fields a job leaves unstated. Shown beside the match, never hidden.
CREATE OR REPLACE FUNCTION unverified_fields(j jobs, c compiled_constraints) RETURNS unverified_flag[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT array_remove(ARRAY[
    CASE WHEN (c.countries IS NOT NULL OR c.authorized_countries IS NOT NULL) AND j.country IS NULL
         THEN 'location_not_stated'::unverified_flag END,
    CASE WHEN c.arrangements IS NOT NULL AND cardinality(j.work_arrangements) = 0
         THEN 'arrangement_not_stated'::unverified_flag END,
    CASE WHEN c.arrangements IS NOT NULL AND cardinality(j.work_arrangements) > 0
              AND NOT (j.work_arrangements <@ c.arrangements)
         THEN 'arrangement_ambiguous'::unverified_flag END,
    CASE WHEN c.salary_floor IS NOT NULL AND j.salary_max IS NULL
         THEN 'salary_not_stated'::unverified_flag END,
    CASE WHEN c.salary_floor IS NOT NULL AND j.salary_max IS NOT NULL AND j.salary_currency <> c.salary_currency
         THEN 'salary_currency_differs'::unverified_flag END,
    CASE WHEN c.employment_types IS NOT NULL AND j.employment_type = 'unknown'
         THEN 'employment_type_not_stated'::unverified_flag END,
    CASE WHEN c.seniorities IS NOT NULL AND j.seniority = 'unknown'
         THEN 'seniority_not_stated'::unverified_flag END
  ], NULL)
$$;
