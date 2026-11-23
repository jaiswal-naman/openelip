-- Integration fix (PR #4): publishing and closing a role ran on the worker's login inside the API process. The API
-- container holds only the API login, and the worker's login serves every person (its policies are USING (true)), so it
-- stays in the worker container. These three functions are the only writes those two actions need that app_api may not
-- make itself: the role's unlisted job (jobs, job_sources and job_requirements belong to the index) and withdrawing a
-- closing role's open asks (app_api moves an ask only as its candidate). Each checks that the caller is an active member
-- of the role's organization, as the roles policy does, and refuses an outside agent's token. Everything else publish and
-- close do (the role row, the ask job, the audit row) runs as app_api under its own policies and grants.

CREATE OR REPLACE FUNCTION direct_job_open(p_role_id uuid, p_description text, p_requirements jsonb,
                                           p_apply_url text, p_content_hash text, p_prompt_version text)
RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  r roles%ROWTYPE;
  v_company text;
  v_source bigint;
  v_job bigint;
BEGIN
  SELECT * INTO r FROM roles WHERE id = p_role_id FOR UPDATE;
  IF NOT FOUND OR NOT app_is_org_member(r.organization_id) OR app_agent_token_id() IS NOT NULL THEN
    RAISE EXCEPTION 'direct_job_open: not a member of the organization of role %', p_role_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF r.status <> 'draft' OR r.job_id IS NOT NULL THEN
    RAISE EXCEPTION 'direct_job_open: role % is % and cannot get a new job', p_role_id, r.status
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT o.name INTO v_company FROM organizations o WHERE o.id = r.organization_id;
  -- One direct source per organization (token = the organization id), made on first use; the fetch never visits it.
  INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
  VALUES ('direct', r.organization_id::text, v_company, r.organization_id, false)
  ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name
  RETURNING id INTO v_source;
  INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements, employment_type,
                    seniority, salary_min, salary_max, salary_currency, visa_sponsorship, description, apply_url,
                    content_hash, dedupe_key, content_version, requirements_version, is_listed)
  VALUES (v_source, p_role_id::text, r.title, v_company, r.country, r.city, r.work_arrangements, r.employment_type,
          r.seniority, r.salary_min, r.salary_max, r.salary_currency, r.visa_sponsorship, p_description, p_apply_url,
          p_content_hash, 'direct:' || p_role_id::text, 1, 1, false)
  RETURNING id INTO v_job;
  INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span, min_years,
                                prompt_version)
  SELECT v_job, 1, (e->>'ordinal')::int, (e->>'importance')::requirement_importance,
         (e->>'kind')::requirement_kind, e->>'text', int4range((e->>'start')::int, (e->>'end')::int),
         (e->>'min_years')::int, p_prompt_version
  FROM jsonb_array_elements(p_requirements) AS e;
  RETURN v_job;
END;
$$;

CREATE OR REPLACE FUNCTION direct_job_close(p_job_id bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF app_agent_token_id() IS NOT NULL
     OR NOT EXISTS (SELECT 1 FROM roles r WHERE r.job_id = p_job_id AND app_is_org_member(r.organization_id)) THEN
    RAISE EXCEPTION 'direct_job_close: not a member of the organization of job %', p_job_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = p_job_id AND status = 'open';
END;
$$;

CREATE OR REPLACE FUNCTION withdraw_role_asks(p_role_id uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_withdrawn integer;
BEGIN
  IF app_agent_token_id() IS NOT NULL
     OR NOT EXISTS (SELECT 1 FROM roles r WHERE r.id = p_role_id AND app_is_org_member(r.organization_id)) THEN
    RAISE EXCEPTION 'withdraw_role_asks: not a member of the organization of role %', p_role_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- introductions_guard still checks the move: only an 'asked' row may become 'withdrawn'.
  UPDATE introductions SET status = 'withdrawn' WHERE role_id = p_role_id AND status = 'asked';
  GET DIAGNOSTICS v_withdrawn = ROW_COUNT;
  RETURN v_withdrawn;
END;
$$;

REVOKE EXECUTE ON FUNCTION direct_job_open(uuid, text, jsonb, text, text, text), direct_job_close(bigint),
  withdraw_role_asks(uuid) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION direct_job_open(uuid, text, jsonb, text, text, text), direct_job_close(bigint),
  withdraw_role_asks(uuid) TO app_api;
