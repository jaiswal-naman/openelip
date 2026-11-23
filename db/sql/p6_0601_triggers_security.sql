-- Revision p6_0601_hiring, part 3: the guards that keep the hiring desk true, the organization functions, row-level
-- security and grants. Read by db/versions/p6_0601_hiring.py, which runs it as app_owner (so every object below is owned
-- by app_owner and every grant is issued by its owner, ARCHITECTURE 7.1). DATA_MODEL 4.7, 5.3, 5.4 and 5.7.
--
-- Not here on purpose: twin_sessions_check, twin_messages_check and agent_tokens_check, and the Twin and agent-token
-- policies (Phases 8 and 9). The three Phase 6 additions to the document are marked "Phase 6 addition": the G7 condition
-- of interest_signals_check, the as_of key of role_preview, and the functions my_org_invitations and introduction_role.

-- DATA_MODEL 4.7, invariant 4. Consent before anything about a candidate reaches an employer.

CREATE OR REPLACE FUNCTION consent_is_active(p_consent_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM consents g
    WHERE g.id = p_consent_id AND g.action = 'granted'
      AND NOT EXISTS (SELECT 1 FROM consents w WHERE w.withdraws_id = g.id))
$$;

CREATE OR REPLACE FUNCTION has_active_consent(p_user_id uuid, p_purpose consent_purpose,
                                              p_organization_id uuid DEFAULT NULL, p_role_id uuid DEFAULT NULL)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM consents g
    WHERE g.user_id = p_user_id AND g.purpose = p_purpose AND g.action = 'granted'
      AND g.organization_id IS NOT DISTINCT FROM p_organization_id
      AND g.role_id IS NOT DISTINCT FROM p_role_id
      AND NOT EXISTS (SELECT 1 FROM consents w WHERE w.withdraws_id = g.id))
$$;

-- An organization with an answer past due may not start new introductions (HIR-06).
CREATE OR REPLACE FUNCTION organization_has_overdue_answers(p_organization_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM introductions i
    WHERE i.organization_id = p_organization_id AND i.status = 'accepted'
      AND i.employer_answer IS NULL AND i.answer_due_at < now())
$$;

-- DATA_MODEL 5.3. Membership, as the policies below ask it. Both read org_members as its owner, so no policy loops.
CREATE OR REPLACE FUNCTION app_is_org_member(p_organization_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM org_members m
                 WHERE m.organization_id = p_organization_id AND m.user_id = app_user_id() AND m.status = 'active')
$$;

CREATE OR REPLACE FUNCTION app_is_org_admin(p_organization_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM org_members m
                 WHERE m.organization_id = p_organization_id AND m.user_id = app_user_id()
                   AND m.status = 'active' AND m.role IN ('owner', 'admin'))
$$;

CREATE OR REPLACE FUNCTION introductions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  r roles;
  j jobs;
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN NEW;
  END IF;

  IF TG_OP = 'INSERT' THEN
    NEW.expires_at := NEW.asked_at + interval '7 days';     -- HIR-04, set here so no caller can choose it
    SELECT * INTO r FROM roles WHERE id = NEW.role_id AND organization_id = NEW.organization_id;
    IF r.id IS NULL OR r.status <> 'open' THEN
      RAISE EXCEPTION 'introductions need an open role of this organization' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO j FROM jobs WHERE id = r.job_id;
    PERFORM 1 FROM match_decisions d
    WHERE d.id = NEW.match_decision_id AND d.user_id = NEW.user_id
      AND d.job_id = r.job_id AND d.outcome = 'introduced';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'introductions need an introduced match decision for this candidate and role'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status <> 'asked' THEN
      RAISE EXCEPTION 'introductions start as asked' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT has_active_consent(NEW.user_id, 'open_to_introductions') THEN
      RAISE EXCEPTION 'consent_missing: candidate is not open to introductions' USING ERRCODE = 'check_violation';
    END IF;
    -- Invariant 1 and HIR-07 on the hiring side: the same filter, the role's index row, the candidate's constraints.
    IF constraint_violations(j, compile_constraints(NEW.user_id)) <> '{}' THEN
      RAISE EXCEPTION 'hard_constraint: the role breaks a constraint of this candidate' USING ERRCODE = 'check_violation';
    END IF;
    IF organization_has_overdue_answers(NEW.organization_id) THEN
      RAISE EXCEPTION 'answer_clock: this organization has overdue answers' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- UPDATE: what was asked and shared never changes.
  IF (NEW.organization_id, NEW.role_id, NEW.user_id, NEW.match_decision_id, NEW.share_preview,
      NEW.asked_at, NEW.expires_at, NEW.created_at)
     IS DISTINCT FROM
     (OLD.organization_id, OLD.role_id, OLD.user_id, OLD.match_decision_id, OLD.share_preview,
      OLD.asked_at, OLD.expires_at, OLD.created_at) THEN
    RAISE EXCEPTION 'introduction content is immutable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    IF OLD.status <> 'asked' OR NEW.status = 'asked' THEN
      RAISE EXCEPTION 'introduction cannot move from % to %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IN ('accepted', 'declined') THEN
      NEW.responded_at := now();
    END IF;
    IF NEW.status = 'accepted' THEN
      -- HIR-06: the answer clock starts now, five business days in the organization's time zone.
      NEW.answer_due_at := add_business_days(now(), 5,
                             (SELECT o.timezone FROM organizations o WHERE o.id = NEW.organization_id));
      IF now() >= OLD.expires_at THEN
        RAISE EXCEPTION 'introduction expired at %', OLD.expires_at USING ERRCODE = 'check_violation';
      END IF;
      IF NOT has_active_consent(NEW.user_id, 'introduction', NEW.organization_id, NEW.role_id) THEN
        RAISE EXCEPTION 'consent_missing: accepting needs the candidate''s consent for this employer and role'
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;
    IF NEW.status = 'expired' AND now() < OLD.expires_at THEN
      RAISE EXCEPTION 'introduction has not expired yet' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF OLD.answer_due_at IS NOT NULL AND NEW.answer_due_at IS DISTINCT FROM OLD.answer_due_at THEN
    RAISE EXCEPTION 'answer_due_at is set once' USING ERRCODE = 'check_violation';
  END IF;
  IF (NEW.employer_answer, NEW.employer_answer_reason, NEW.employer_answered_by, NEW.employer_answered_at)
     IS DISTINCT FROM
     (OLD.employer_answer, OLD.employer_answer_reason, OLD.employer_answered_by, OLD.employer_answered_at) THEN
    IF OLD.status <> 'accepted' OR OLD.employer_answer IS NOT NULL THEN
      RAISE EXCEPTION 'the employer answers once, after acceptance' USING ERRCODE = 'check_violation';
    END IF;
    NEW.employer_answered_at := now();
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER introductions_guard BEFORE INSERT OR UPDATE ON introductions
  FOR EACH ROW EXECUTE FUNCTION introductions_guard();

-- HIR-05: a shortlist entry exists only for an accepted introduction with live consent,
-- and carries exactly what the candidate saw.
CREATE OR REPLACE FUNCTION shortlist_entries_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM 1 FROM introductions i
  WHERE i.id = NEW.introduction_id AND i.status = 'accepted'
    AND i.user_id = NEW.user_id AND i.organization_id = NEW.organization_id AND i.role_id = NEW.role_id
    AND i.share_preview = NEW.shared_payload;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'consent_missing: shortlist entries need an accepted introduction and its exact preview'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT has_active_consent(NEW.user_id, 'introduction', NEW.organization_id, NEW.role_id) THEN
    RAISE EXCEPTION 'consent_missing: introduction consent withdrawn'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER shortlist_entries_check BEFORE INSERT ON shortlist_entries
  FOR EACH ROW EXECUTE FUNCTION shortlist_entries_check();

-- HIR-08: an organization's signal rides on one of its own introductions.
-- Phase 6 addition (owner decision G7, index deviation 16): only on one the candidate has accepted, so an employer
-- cannot signal interest in a candidate who has not answered the ask. The API refuses it as well.
CREATE OR REPLACE FUNCTION interest_signals_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NEW.sender_side = 'organization' THEN
    PERFORM 1 FROM introductions i
    WHERE i.id = NEW.introduction_id AND i.organization_id = NEW.organization_id
      AND i.role_id = NEW.role_id AND i.user_id = NEW.user_id
      AND i.status = 'accepted';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'an organization signal must ride on its own introduction' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER interest_signals_check BEFORE INSERT ON interest_signals
  FOR EACH ROW EXECUTE FUNCTION interest_signals_check();

-- DATA_MODEL 5.3. Entry points that run before a membership exists, or that must not open a whole table to a role.
CREATE OR REPLACE FUNCTION create_organization(p_name text, p_timezone text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  oid uuid;
BEGIN
  IF app_user_id() IS NULL OR app_agent_token_id() IS NOT NULL THEN
    RAISE EXCEPTION 'a signed-in person creates an organization' USING ERRCODE = 'insufficient_privilege';
  END IF;
  INSERT INTO organizations (name, timezone, created_by) VALUES (p_name, p_timezone, app_user_id()) RETURNING id INTO oid;
  INSERT INTO org_members (organization_id, user_id, role, status, joined_at)
  VALUES (oid, app_user_id(), 'owner', 'active', now());
  RETURN oid;
END;
$$;

CREATE OR REPLACE FUNCTION accept_org_invitation(p_member_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  UPDATE org_members m SET user_id = app_user_id(), status = 'active', joined_at = now()
  WHERE m.id = p_member_id AND m.status = 'invited'
    AND lower(m.invited_email) = (SELECT lower(u.email) FROM users u WHERE u.id = app_user_id());
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no open invitation for this account' USING ERRCODE = 'insufficient_privilege';
  END IF;
END;
$$;

-- Colleagues' names and emails, without opening the users table to other people.
CREATE OR REPLACE FUNCTION org_directory(p_organization_id uuid)
RETURNS TABLE (member_id uuid, user_id uuid, display_name text, email text, role org_role, status member_status)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT m.id, m.user_id, u.display_name, coalesce(u.email, m.invited_email), m.role, m.status
  FROM org_members m LEFT JOIN users u ON u.id = m.user_id
  WHERE m.organization_id = p_organization_id AND m.status <> 'removed' AND app_is_org_member(p_organization_id)
$$;

-- HIR-03: the anonymous preview. Counts only; any count from 1 to 4 (and 0 for the asks in flight, see below) is reported as "fewer than 5" (assumption).
CREATE OR REPLACE FUNCTION suppress_small(n bigint) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN n = 0 OR n >= 5 THEN to_jsonb(n) ELSE to_jsonb('fewer than 5'::text) END
$$;

-- W4: the counts of asks that can move (waiting, no) hide 0 as well, so a lone ask moving from waiting to declined does
-- not show as a count change. said_yes stays exact: each of those people is on the shortlist.
CREATE OR REPLACE FUNCTION suppress_below_five(n bigint) RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN n >= 5 THEN to_jsonb(n) ELSE to_jsonb('fewer than 5'::text) END
$$;

-- Phase 6 addition: the key as_of, the time the latest finished role run ended (NULL before the first one), so the
-- employer can see how old the counts are.
CREATE OR REPLACE FUNCTION role_preview(p_role_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  WITH r AS (
    SELECT ro.id FROM roles ro WHERE ro.id = p_role_id AND app_is_org_member(ro.organization_id)
  ),
  latest AS (
    SELECT mr.id, mr.finished_at FROM match_runs mr JOIN r ON mr.role_id = r.id
    WHERE mr.status = 'completed' ORDER BY mr.started_at DESC LIMIT 1
  ),
  d AS (
    SELECT count(*) FILTER (WHERE d.outcome <> 'filtered')   AS eligible,
           count(*) FILTER (WHERE d.outcome = 'introduced') AS selected
    FROM match_decisions d JOIN latest ON d.match_run_id = latest.id
  ),
  i AS (
    SELECT count(*) FILTER (WHERE i.status = 'asked')                   AS waiting,
           count(*) FILTER (WHERE i.status = 'accepted')                AS accepted,
           count(*) FILTER (WHERE i.status IN ('declined', 'expired'))  AS said_no
    FROM introductions i JOIN r ON i.role_id = r.id
  )
  SELECT jsonb_build_object(
           'eligible_candidates', suppress_small(d.eligible),
           'selected_to_ask',     suppress_small(d.selected),
           'waiting_for_reply',   suppress_below_five(i.waiting),
           'said_yes',            i.accepted,              -- each of these is already on the shortlist
           'said_no_or_expired',  suppress_below_five(i.said_no),
           'as_of',               (SELECT l.finished_at FROM latest l))
  FROM d, i
  WHERE EXISTS (SELECT 1 FROM r)
$$;

-- Phase 6 addition (index deviation 18): the open invitations of the caller's own address. An invited person is not a
-- member yet, so row-level security hides the invitation row from them; this is how /v1/me lists it.
CREATE OR REPLACE FUNCTION my_org_invitations()
RETURNS TABLE (member_id uuid, organization_id uuid, organization_name text, role org_role, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT m.id, m.organization_id, o.name, m.role, m.created_at
  FROM org_members m
  JOIN organizations o ON o.id = m.organization_id
  JOIN users u ON u.id = app_user_id()
  WHERE m.status = 'invited' AND lower(m.invited_email) = lower(u.email)
    AND app_agent_token_id() IS NULL
  ORDER BY m.created_at, m.id
$$;

-- Phase 6 addition (index deviation 18): the role and organization behind one of the caller's own introductions. A
-- candidate has no row access to roles or organizations, yet the ask must name the employer, the role and its pay range.
CREATE OR REPLACE FUNCTION introduction_role(p_introduction_id uuid)
RETURNS TABLE (organization_name text, role_title text, job_id bigint, country char(2), city text,
               work_arrangements work_arrangement[], salary_min integer, salary_max integer, salary_currency char(3),
               is_synthetic boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT o.name, r.title, r.job_id, r.country, r.city, r.work_arrangements, r.salary_min, r.salary_max,
         r.salary_currency, o.is_synthetic
  FROM introductions i
  JOIN roles r ON r.id = i.role_id
  JOIN organizations o ON o.id = i.organization_id
  WHERE i.id = p_introduction_id AND i.user_id = app_user_id() AND app_agent_token_id() IS NULL
$$;

-- These two read the hiring side, which the match role may not (DATA_MODEL 5.1), so they are for the API and the worker
-- only. The functions copied from DATA_MODEL above keep PostgreSQL's default EXECUTE for PUBLIC, as section 5.7 says.
REVOKE EXECUTE ON FUNCTION my_org_invitations(), introduction_role(uuid) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION my_org_invitations(), introduction_role(uuid) TO app_api, app_worker;

-- DATA_MODEL 5.4. Organization isolation (HIR-01).
ALTER TABLE organizations     ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members       ENABLE ROW LEVEL SECURITY;
ALTER TABLE roles             ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE introductions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE shortlist_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE interest_signals  ENABLE ROW LEVEL SECURITY;

CREATE POLICY member_read  ON organizations FOR SELECT TO app_api
  USING (app_is_org_member(id) AND app_agent_token_id() IS NULL);
CREATE POLICY admin_update ON organizations FOR UPDATE TO app_api
  USING (app_is_org_admin(id) AND app_agent_token_id() IS NULL);

CREATE POLICY member_read ON org_members FOR SELECT TO app_api
  USING (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL);
CREATE POLICY admin_write ON org_members TO app_api
  USING (app_is_org_admin(organization_id) AND app_agent_token_id() IS NULL)
  WITH CHECK (app_is_org_admin(organization_id) AND app_agent_token_id() IS NULL);

CREATE POLICY member_all ON roles TO app_api
  USING (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL)
  WITH CHECK (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL);
CREATE POLICY member_all ON role_requirements TO app_api
  USING (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL)
  WITH CHECK (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL);

-- Introductions: the candidate sees and answers their own asks. The employer sees nothing until the
-- candidate accepts, then sees and answers that row once. There is no employer policy for 'asked' rows.
CREATE POLICY candidate_read   ON introductions FOR SELECT TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY candidate_answer ON introductions FOR UPDATE TO app_api
  USING (user_id = app_user_id() AND status = 'asked' AND app_agent_token_id() IS NULL)
  WITH CHECK (user_id = app_user_id());
CREATE POLICY employer_read    ON introductions FOR SELECT TO app_api
  USING (app_is_org_member(organization_id) AND status = 'accepted' AND app_agent_token_id() IS NULL);
CREATE POLICY employer_answer  ON introductions FOR UPDATE TO app_api
  USING (app_is_org_member(organization_id) AND status = 'accepted' AND employer_answer IS NULL
         AND app_agent_token_id() IS NULL)
  WITH CHECK (app_is_org_member(organization_id) AND employer_answered_by = app_user_id());

-- The candidate reads their own entry and creates it when they accept; they never rewrite or move it afterwards
-- (the employer reads shared_payload as the candidate agreed to it), so there is no UPDATE or DELETE policy.
CREATE POLICY candidate_own ON shortlist_entries FOR SELECT TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY candidate_insert ON shortlist_entries FOR INSERT TO app_api
  WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY employer_read ON shortlist_entries FOR SELECT TO app_api   -- disappears if consent is withdrawn
  USING (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL
         AND has_active_consent(user_id, 'introduction', organization_id, role_id));

CREATE POLICY candidate_read ON interest_signals FOR SELECT TO app_api
  USING (user_id = app_user_id() AND app_agent_token_id() IS NULL);
CREATE POLICY candidate_send ON interest_signals FOR INSERT TO app_api
  WITH CHECK (user_id = app_user_id() AND sender_side = 'candidate' AND sent_by = app_user_id()
              AND app_agent_token_id() IS NULL);
CREATE POLICY employer_read  ON interest_signals FOR SELECT TO app_api
  USING (app_is_org_member(organization_id) AND app_agent_token_id() IS NULL
         AND (sender_side = 'organization'
              OR EXISTS (SELECT 1 FROM introductions i
                         WHERE i.organization_id = interest_signals.organization_id
                           AND i.role_id = interest_signals.role_id AND i.user_id = interest_signals.user_id
                           AND i.status = 'accepted')));
CREATE POLICY employer_send  ON interest_signals FOR INSERT TO app_api
  WITH CHECK (app_is_org_member(organization_id) AND sender_side = 'organization' AND sent_by = app_user_id()
              AND app_agent_token_id() IS NULL);

-- The worker serves every person, so it sees every row; privileges below still bind it.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['organizations', 'org_members', 'roles', 'role_requirements', 'introductions',
                           'shortlist_entries', 'interest_signals'] LOOP
    EXECUTE format('CREATE POLICY worker_all ON %I TO app_worker USING (true) WITH CHECK (true)', t);
  END LOOP;
END;
$$;

-- DATA_MODEL 5.7. The grant is the boundary; triggers are the second line. Guarded table: the worker creates the ask;
-- people only move it forward, and every move is checked by introductions_guard.
GRANT SELECT, UPDATE         ON introductions TO app_api;
GRANT SELECT, INSERT, UPDATE ON introductions TO app_worker;

-- Mutable tables, scoped by the policies above.
GRANT SELECT, INSERT, UPDATE, DELETE ON
  organizations, org_members, roles, role_requirements, interest_signals
  TO app_api, app_worker;
-- A shortlist entry is written once, by the candidate's acceptance, and never changed: no UPDATE or DELETE for the
-- API login (erasure removes it by the foreign key's cascade, which needs no grant).
GRANT SELECT, INSERT ON shortlist_entries TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON shortlist_entries TO app_worker;
