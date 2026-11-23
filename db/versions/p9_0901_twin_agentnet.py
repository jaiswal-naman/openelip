# ruff: noqa: E501  (the SQL below is copied from DATA_MODEL and the Phase 7 revision, whose lines are longer)
"""The Twin and the agent network: four tables, their guards, row-level security and grants (09-01).

DATA_MODEL 3.11 (tables), 3.14 (three cross foreign keys), 3.15 (the Twin thread trigger), 4.x (twin_messages_check,
twin_sessions_check, agent_tokens_check), 4.5 (append-only), 5.2 to 5.5 (request context, resolve_agent_token and the
policies) and 5.7 (grants), with the seven amendments of 09-INDEX "DDL amendments":

  (a) twin_messages.topic is required only on a Twin message: an employer's question is stored without one
  (b) agent_actions_guard: a twin_answer whose payload kind is 'relay' (the candidate's own reply) is not stopped by the
      dial 'never', and always needs an approved approval; everything else is as Phase 7 left it
  (c) five policies: agent_introductions, agent_shortlist, agent_roles, agent_organizations (organization tokens, accepted
      introductions only; agent_organizations is the token's own organization row, for the name in labels)
      and org_token_calls (admins read their organization's token calls in audit_log)
  (d) touch_agent_token, role_ask_bucket and twin_message_citations (SECURITY DEFINER; EXECUTE to app_api only)
  (e) queue_jobs keeps no row-level security; DATA_MODEL's agent_request_introduction policy is not created
  (f) an 'answered' message is refused for scope only when the topic's permission is 'never'
  (g) at most one final Twin message (answered, relayed, declined) per question

Two further differences, both narrowings made for the API login (which the employer's and an outside agent's requests
use): app_api may only SELECT and INSERT twin_sessions (an employer must not close or delete a session; the worker does),
and may only UPDATE agent_tokens.revoked_at. twin_sessions also gets an index on introduction_id.

The SQL runs as app_owner. Every type, function and policy is guarded or CREATE OR REPLACE, so the revision can be run
again on a database where an earlier attempt stopped. downgrade() exists for the CI migration check on a scratch
database; its first statement refuses a database called app, because it drops the four tables.
"""

from alembic import op

revision = "p9_0901_twin_agentnet"
down_revision = "p8_0801_wind_tunnel"
branch_labels = None
depends_on = None

# --------------------------------------------------------------------------------------------------------------------
# Types and tables (3.11)
# --------------------------------------------------------------------------------------------------------------------

ENUMS = {
    "twin_topic": (
        "experience",
        "skills",
        "education",
        "projects",
        "availability",
        "work_authorization",
        "location",
        "expected_pay",
        "motivation",
        "current_pay",
        "contact_details",
        "other",
    ),
    "twin_permission": ("answer", "ask_me", "never"),
    "twin_author": ("employer", "twin", "candidate"),
    "twin_answer_kind": ("answered", "unknown", "declined", "relayed"),
    "twin_session_status": ("open", "closed"),
    "token_principal": ("candidate", "organization"),
    "token_scope": ("matches:read", "explanations:read", "twin:ask", "introductions:request"),
}


def _enum(name: str, labels: tuple[str, ...]) -> str:
    values = ", ".join(f"'{label}'" for label in labels)
    return (
        f"DO $enum$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{name}') THEN "
        f"CREATE TYPE {name} AS ENUM ({values}); END IF; END $enum$;"
    )


TABLES = """
-- What the Twin may answer (TWN-03). No row means 'ask_me'.
CREATE TABLE twin_scopes (
  user_id    uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  topic      twin_topic      NOT NULL,
  permission twin_permission NOT NULL,
  updated_at timestamptz     NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, topic),
  CONSTRAINT twin_scopes_never_pay_or_contact
    CHECK (topic NOT IN ('current_pay', 'contact_details') OR permission = 'never')
);

-- A consent-scoped credential for an outside AI agent (NET-01). Only the hash is stored.
CREATE TABLE agent_tokens (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  principal       token_principal NOT NULL,
  user_id         uuid            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  organization_id uuid            REFERENCES organizations(id) ON DELETE CASCADE,
  consent_id      uuid            REFERENCES consents(id) ON DELETE CASCADE,
  client_name     text            NOT NULL,
  token_sha256    bytea           NOT NULL UNIQUE,
  scopes          token_scope[]   NOT NULL CHECK (cardinality(scopes) >= 1),
  expires_at      timestamptz     NOT NULL,
  revoked_at      timestamptz,
  last_used_at    timestamptz,
  created_at      timestamptz     NOT NULL DEFAULT now(),
  CHECK ((principal = 'organization') = (organization_id IS NOT NULL)),
  CHECK ((principal = 'candidate') = (consent_id IS NOT NULL)),
  CONSTRAINT agent_tokens_principal_scopes CHECK (
    (principal = 'candidate' AND scopes <@ ARRAY['matches:read', 'explanations:read']::token_scope[])
    OR (principal = 'organization' AND scopes <@ ARRAY['twin:ask', 'introductions:request']::token_scope[]))
);

-- A conversation between an employer (or its agent) and a candidate's Twin, after acceptance (TWN-01).
CREATE TABLE twin_sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  introduction_id uuid                NOT NULL REFERENCES introductions(id) ON DELETE CASCADE,
  user_id         uuid                NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  organization_id uuid                NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  opened_by       uuid                REFERENCES users(id) ON DELETE SET NULL,
  agent_token_id  uuid                REFERENCES agent_tokens(id) ON DELETE SET NULL,
  status          twin_session_status NOT NULL DEFAULT 'open',
  created_at      timestamptz         NOT NULL DEFAULT now(),
  closed_at       timestamptz,
  UNIQUE (id, user_id, organization_id),
  CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
CREATE INDEX twin_sessions_introduction_idx ON twin_sessions (introduction_id);

-- Every question and answer, readable in full by the candidate (TWN-04). Append-only.
CREATE TABLE twin_messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      uuid             NOT NULL,
  user_id         uuid             NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  organization_id uuid             NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  author          twin_author      NOT NULL,
  body            text             NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),
  topic           twin_topic,
  answer_kind     twin_answer_kind,
  cited_fact_ids  uuid[]           NOT NULL DEFAULT '{}',
  in_reply_to     uuid             REFERENCES twin_messages(id) ON DELETE CASCADE,
  agent_action_id bigint,
  created_at      timestamptz      NOT NULL DEFAULT now(),
  FOREIGN KEY (session_id, user_id, organization_id)
    REFERENCES twin_sessions (id, user_id, organization_id) ON DELETE CASCADE,
  CHECK ((author = 'twin') = (answer_kind IS NOT NULL)),
  -- amendment (a): was (author = 'candidate' OR topic IS NOT NULL); an employer's question has no topic yet
  CHECK (author <> 'twin' OR topic IS NOT NULL),
  CHECK (answer_kind IS DISTINCT FROM 'answered' OR cardinality(cited_fact_ids) > 0),
  CHECK (answer_kind IS DISTINCT FROM 'relayed' OR in_reply_to IS NOT NULL),
  CHECK (answer_kind NOT IN ('answered', 'relayed') OR agent_action_id IS NOT NULL),
  CONSTRAINT twin_messages_never_pay_or_contact
    CHECK (answer_kind NOT IN ('answered', 'relayed') OR topic NOT IN ('current_pay', 'contact_details'))
);
CREATE INDEX twin_messages_session_idx ON twin_messages (session_id, created_at);
-- amendment (g): at most one final Twin message per question, whatever retries happen
CREATE UNIQUE INDEX twin_messages_one_final ON twin_messages (in_reply_to)
  WHERE author = 'twin' AND answer_kind IN ('answered', 'relayed', 'declined');

-- 3.14 cross foreign keys
ALTER TABLE audit_log     ADD FOREIGN KEY (agent_token_id)  REFERENCES agent_tokens(id)  ON DELETE SET NULL;
ALTER TABLE agent_runs    ADD FOREIGN KEY (twin_session_id) REFERENCES twin_sessions(id) ON DELETE CASCADE;
ALTER TABLE twin_messages ADD FOREIGN KEY (agent_action_id) REFERENCES agent_actions(id) ON DELETE CASCADE;
"""

# --------------------------------------------------------------------------------------------------------------------
# Functions and triggers (4.x, 5.2, 3.15, amendments b, d, f)
# --------------------------------------------------------------------------------------------------------------------

FUNCTIONS = """
-- 5.2: true for a person's own request; for an outside agent, only if its token carries the scope.
CREATE OR REPLACE FUNCTION app_scope_ok(p_scope token_scope) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN app_agent_token_id() IS NULL THEN true
    WHEN p_scope IS NULL THEN false
    ELSE p_scope::text = ANY (string_to_array(current_setting('app.token_scopes', true), ','))
  END
$$;

-- Runs before any request context exists: a bearer secret's hash in, the token's identity out. Withdrawing the
-- candidate's consent, or the issuer leaving the organization, kills the token with no write.
CREATE OR REPLACE FUNCTION resolve_agent_token(p_token_sha256 bytea)
RETURNS TABLE (token_id uuid, user_id uuid, organization_id uuid, scopes token_scope[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT t.id, t.user_id, t.organization_id, t.scopes
  FROM agent_tokens t
  WHERE t.token_sha256 = p_token_sha256 AND t.revoked_at IS NULL AND t.expires_at > now()
    AND CASE t.principal
          WHEN 'candidate' THEN consent_is_active(t.consent_id)
          ELSE EXISTS (SELECT 1 FROM org_members m
                       WHERE m.organization_id = t.organization_id AND m.user_id = t.user_id AND m.status = 'active')
        END
$$;

-- last_used_at, at most once a minute: a token's own context cannot update its row. Called inside the token's own
-- request context (or by its issuer); anyone else is refused.
CREATE OR REPLACE FUNCTION touch_agent_token(p_token_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM 1 FROM agent_tokens t
  WHERE t.id = p_token_id
    AND (app_agent_token_id() = t.id OR (app_agent_token_id() IS NULL AND app_user_id() = t.user_id));
  IF NOT FOUND THEN
    RAISE EXCEPTION 'touch_agent_token: not this token''s request' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE agent_tokens t SET last_used_at = now()
  WHERE t.id = p_token_id AND (t.last_used_at IS NULL OR t.last_used_at < now() - interval '1 minute');
END;
$$;

-- How many candidates a role's ordinary ask is still waiting on, as a bucket (the preview's suppression: below five
-- reads as one bucket). Members of the role's organization only; an organization token needs introductions:request.
CREATE OR REPLACE FUNCTION role_ask_bucket(p_role_id uuid) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  org uuid;
  n   bigint;
BEGIN
  SELECT r.organization_id INTO org FROM roles r WHERE r.id = p_role_id;
  IF org IS NULL OR NOT app_is_org_member(org) OR NOT app_scope_ok('introductions:request') THEN
    RAISE EXCEPTION 'role_ask_bucket: not a member of the organization of role %', p_role_id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT count(*) INTO n FROM introductions i WHERE i.role_id = p_role_id AND i.status = 'asked';
  RETURN CASE WHEN n < 5 THEN 'under_5' WHEN n < 10 THEN '5_to_9' WHEN n < 25 THEN '10_to_24' ELSE '25_plus' END;
END;
$$;

-- The facts a released answer cites, as the statement text: all an employer may read of the record. The candidate (no
-- token) and an active member of the message's organization with twin:ask (or a person's own request) may call it.
CREATE OR REPLACE FUNCTION twin_message_citations(p_message_id uuid) RETURNS TABLE (fact_id uuid, quote text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT f.id, f.statement
  FROM twin_messages m
  CROSS JOIN LATERAL unnest(m.cited_fact_ids) WITH ORDINALITY AS c(fact_id, position)
  JOIN profile_facts f ON f.id = c.fact_id AND f.user_id = m.user_id
  WHERE m.id = p_message_id AND m.author = 'twin' AND m.answer_kind = 'answered'
    AND ((app_agent_token_id() IS NULL AND m.user_id = app_user_id())
         OR (app_is_org_member(m.organization_id) AND app_scope_ok('twin:ask')))
  ORDER BY c.position
$$;

-- TWN-01: a Twin session opens only on an accepted introduction with live consent.
CREATE OR REPLACE FUNCTION twin_sessions_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  i introductions;
BEGIN
  SELECT * INTO i FROM introductions WHERE id = NEW.introduction_id;
  IF i.id IS NULL OR i.status <> 'accepted' OR i.user_id <> NEW.user_id OR i.organization_id <> NEW.organization_id
     OR NOT has_active_consent(i.user_id, 'introduction', i.organization_id, i.role_id) THEN
    RAISE EXCEPTION 'consent_missing: the Twin answers only after the candidate accepts' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

-- Twin answers cite only the candidate's usable facts, and only on topics the candidate has not closed (TWN-01..03).
-- Amendment (f): 'answered' is refused only for a topic whose permission is 'never' (pay and contact always are).
CREATE OR REPLACE FUNCTION twin_messages_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  sess       record;
  permission twin_permission;
  resolved   integer;
BEGIN
  SELECT s.status, i.role_id INTO sess
  FROM twin_sessions s JOIN introductions i ON i.id = s.introduction_id
  WHERE s.id = NEW.session_id;
  IF sess.status IS DISTINCT FROM 'open' THEN
    RAISE EXCEPTION 'twin session is closed' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.author <> 'twin' AND (NEW.agent_action_id IS NOT NULL OR cardinality(NEW.cited_fact_ids) > 0) THEN
    RAISE EXCEPTION 'only a Twin message carries an action or cited facts' USING ERRCODE = 'check_violation';
  END IF;
  -- Invariant 5: a Twin answer reaches the employer only as an executed outside action.
  IF NEW.agent_action_id IS NOT NULL THEN
    PERFORM 1 FROM agent_actions a
    WHERE a.id = NEW.agent_action_id AND a.user_id = NEW.user_id
      AND a.action_type = 'twin_answer' AND a.status = 'executed';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'two_key: twin answer is not an executed twin_answer action' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  -- Invariant 4: the candidate's consent for this employer and role must still be active.
  IF NOT has_active_consent(NEW.user_id, 'introduction', NEW.organization_id, sess.role_id) THEN
    RAISE EXCEPTION 'consent_missing: introduction consent withdrawn' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.in_reply_to IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM twin_messages m WHERE m.id = NEW.in_reply_to AND m.session_id = NEW.session_id) THEN
    RAISE EXCEPTION 'reply is not in this session' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.answer_kind IN ('answered', 'relayed') THEN
    SELECT s.permission INTO permission FROM twin_scopes s WHERE s.user_id = NEW.user_id AND s.topic = NEW.topic;
    permission := coalesce(permission,
                           CASE WHEN NEW.topic IN ('current_pay', 'contact_details') THEN 'never'::twin_permission
                                ELSE 'ask_me' END);
    IF permission = 'never' THEN
      RAISE EXCEPTION 'scope_denied: topic % is never answered', NEW.topic USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  SELECT count(*) INTO resolved FROM profile_facts f
  WHERE f.id = ANY (NEW.cited_fact_ids) AND f.user_id = NEW.user_id
    AND f.confirmed_at IS NOT NULL AND f.rejected_at IS NULL AND f.superseded_at IS NULL;
  IF resolved <> cardinality(NEW.cited_fact_ids) THEN
    RAISE EXCEPTION 'citation_unresolved: twin answer' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;

-- NET-01: a candidate's agent token is bound to that candidate's live 'agent_access' grant;
-- an organization's token is issued only by one of its admins.
CREATE OR REPLACE FUNCTION agent_tokens_check() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NEW.principal = 'candidate' THEN
    PERFORM 1 FROM consents c
    WHERE c.id = NEW.consent_id AND c.user_id = NEW.user_id AND c.purpose = 'agent_access';
    IF NOT FOUND OR NOT consent_is_active(NEW.consent_id) THEN
      RAISE EXCEPTION 'consent_missing: agent token needs this candidate''s agent_access grant'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    PERFORM 1 FROM org_members m
    WHERE m.organization_id = NEW.organization_id AND m.user_id = NEW.user_id
      AND m.status = 'active' AND m.role IN ('owner', 'admin');
    IF NOT FOUND THEN
      RAISE EXCEPTION 'organization tokens are issued by an owner or admin' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

-- A Twin question (an employer's message) is its own LangGraph thread, keyed by the question's id (3.15).
CREATE OR REPLACE FUNCTION twin_questions_delete_thread() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM langgraph_delete_thread('twin:' || OLD.id);
  RETURN NULL;
END;
$$;

CREATE TRIGGER twin_sessions_check BEFORE INSERT ON twin_sessions
  FOR EACH ROW EXECUTE FUNCTION twin_sessions_check();
CREATE TRIGGER twin_messages_check BEFORE INSERT ON twin_messages
  FOR EACH ROW EXECUTE FUNCTION twin_messages_check();
CREATE TRIGGER agent_tokens_check BEFORE INSERT ON agent_tokens
  FOR EACH ROW EXECUTE FUNCTION agent_tokens_check();
CREATE TRIGGER twin_messages_append_only BEFORE UPDATE OR DELETE ON twin_messages
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER twin_questions_delete_thread AFTER DELETE ON twin_messages
  FOR EACH ROW WHEN (OLD.author = 'employer') EXECUTE FUNCTION twin_questions_delete_thread();

REVOKE EXECUTE ON FUNCTION twin_sessions_check(), twin_messages_check(), agent_tokens_check(),
                           twin_questions_delete_thread() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION resolve_agent_token(bytea), touch_agent_token(uuid), role_ask_bucket(uuid),
                           twin_message_citations(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_agent_token(bytea), touch_agent_token(uuid), role_ask_bucket(uuid),
                          twin_message_citations(uuid) TO app_api;
"""

# The Phase 7 guard (db/sql/p7_0701_triggers_security.sql), written once. The two places the Twin amendment (b) changes
# are the markers {NEVER} and {APPROVAL}; GUARD_PHASE_7 fills them with the Phase 7 text, GUARD_PHASE_9 with the new.
GUARD_TEMPLATE = """
CREATE OR REPLACE FUNCTION agent_actions_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  dial autonomy_level;{DECLARE}
BEGIN
  IF current_setting('app.maintenance', true) = 'on' THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'agent_actions is the flight recorder; rows are never deleted' USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('recorded', 'proposed') THEN
      RAISE EXCEPTION 'an action is recorded first and moves forward by update' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  -- UPDATE: content is fixed; only status, executed_at, outcome_code and the one-time undo_until move.
  IF (to_jsonb(NEW) - ARRAY['status', 'executed_at', 'outcome_code', 'undo_until'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'executed_at', 'outcome_code', 'undo_until']) THEN
    RAISE EXCEPTION 'agent_actions content is immutable' USING ERRCODE = 'insufficient_privilege';
  END IF;
  -- undo_until is set once, from NULL, on the move to scheduled, and then ahead of now by at least a minute (decision 3).
  IF NEW.undo_until IS DISTINCT FROM OLD.undo_until THEN
    IF OLD.undo_until IS NOT NULL THEN
      RAISE EXCEPTION 'undo_until is set once and never changes' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> 'scheduled' OR OLD.status NOT IN ('proposed', 'awaiting_approval') THEN
      RAISE EXCEPTION 'undo_until is set only when the action is scheduled' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.undo_until IS NULL OR NEW.undo_until < now() + interval '1 minute' THEN
      RAISE EXCEPTION 'undo_until must be at least one minute ahead' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.status = OLD.status THEN
    RETURN NEW;
  END IF;
  IF NOT (OLD.status, NEW.status) IN (
       ('proposed', 'awaiting_approval'), ('proposed', 'scheduled'), ('proposed', 'blocked'),
       ('awaiting_approval', 'scheduled'), ('awaiting_approval', 'rejected'),
       ('awaiting_approval', 'expired'), ('awaiting_approval', 'cancelled'), ('awaiting_approval', 'blocked'),
       ('scheduled', 'executed'), ('scheduled', 'failed'), ('scheduled', 'cancelled'), ('scheduled', 'blocked')) THEN
    RAISE EXCEPTION 'agent action cannot move from % to %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
  END IF;

  -- Both keys are checked when the action is cleared to run and again when it runs.
  IF NEW.status IN ('scheduled', 'executed') THEN
    IF NEW.policy_decision = 'deny' THEN
      RAISE EXCEPTION 'two_key: the policy engine denied this action' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM guardian_checks g WHERE g.agent_action_id = NEW.id)
       OR EXISTS (SELECT 1 FROM guardian_checks g WHERE g.agent_action_id = NEW.id AND NOT g.passed) THEN
      RAISE EXCEPTION 'two_key: the Guardian has not passed this action' USING ERRCODE = 'check_violation';
    END IF;
    -- The autonomy dial is read again here, so a policy engine bug cannot skip it. No row = 'ask_first'.
    SELECT s.level INTO dial FROM autonomy_settings s
    WHERE s.user_id = NEW.user_id AND s.action_type = NEW.action_type;
    dial := coalesce(dial, 'ask_first');{RELAY}
    IF dial = 'never'{NEVER} THEN
      RAISE EXCEPTION 'two_key: the candidate set % to never', NEW.action_type USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.policy_decision = 'ask' OR action_always_needs_approval(NEW.action_type) OR dial = 'ask_first'{APPROVAL})
       AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.agent_action_id = NEW.id AND a.status = 'approved') THEN
      RAISE EXCEPTION 'two_key: the candidate has not approved this action' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.status = 'executed' AND NEW.undo_until IS NOT NULL AND now() < NEW.undo_until THEN
    RAISE EXCEPTION 'undo window is still open until %', NEW.undo_until USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$;
"""

GUARD_PHASE_7 = GUARD_TEMPLATE.format(DECLARE="", RELAY="", NEVER="", APPROVAL="")
# Amendment (b): a relay is the candidate's own reply to a question the Twin could not answer. The dial 'never' stops
# the Twin drafting answers from the record, not the candidate speaking, but a relay always needs an approved approval.
GUARD_PHASE_9 = GUARD_TEMPLATE.format(
    DECLARE="\n  is_relay boolean;",
    RELAY="\n    is_relay := NEW.action_type = 'twin_answer' AND coalesce(NEW.payload->>'kind', '') = 'relay';",
    NEVER=" AND NOT is_relay",
    APPROVAL=" OR is_relay",
)

# --------------------------------------------------------------------------------------------------------------------
# Row-level security and grants (5.3, 5.5, 5.7)
# --------------------------------------------------------------------------------------------------------------------


def _policy(name: str, table: str, definition: str) -> str:
    """CREATE POLICY only when the table has none of that name."""
    return (
        f"DO $pol$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' "
        f"AND tablename = '{table}' AND policyname = '{name}') THEN "
        f"EXECUTE $def$CREATE POLICY {name} ON {table} {definition}$def$; END IF; END $pol$;"
    )


POLICIES = [
    # 5.3 candidate isolation for the one table that has no policy of its own below
    _policy(
        "candidate_own",
        "twin_scopes",
        "TO app_api USING (user_id = app_user_id() AND app_agent_token_id() IS NULL) "
        "WITH CHECK (user_id = app_user_id() AND app_agent_token_id() IS NULL)",
    ),
    # 5.5 Twin: the candidate reads everything; the employer (or its agent with 'twin:ask') reads only employer
    # questions and released Twin answers, never the candidate's private replies. A session is opened and never changed
    # by the API login: the policies allow SELECT and INSERT only.
    _policy(
        "candidate_read",
        "twin_sessions",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)",
    ),
    _policy(
        "employer_read",
        "twin_sessions",
        "FOR SELECT TO app_api USING (app_is_org_member(organization_id) AND app_scope_ok('twin:ask'))",
    ),
    _policy(
        "employer_open",
        "twin_sessions",
        "FOR INSERT TO app_api WITH CHECK (app_is_org_member(organization_id) AND app_scope_ok('twin:ask'))",
    ),
    _policy(
        "candidate_read",
        "twin_messages",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_agent_token_id() IS NULL)",
    ),
    _policy(
        "candidate_reply",
        "twin_messages",
        "FOR INSERT TO app_api WITH CHECK (user_id = app_user_id() AND author = 'candidate' "
        "AND app_agent_token_id() IS NULL)",
    ),
    _policy(
        "employer_read",
        "twin_messages",
        "FOR SELECT TO app_api USING (app_is_org_member(organization_id) AND app_scope_ok('twin:ask') "
        "AND author IN ('employer', 'twin'))",
    ),
    _policy(
        "employer_ask",
        "twin_messages",
        "FOR INSERT TO app_api WITH CHECK (app_is_org_member(organization_id) AND app_scope_ok('twin:ask') "
        "AND author = 'employer')",
    ),
    _policy(
        "candidate_own",
        "agent_tokens",
        "TO app_api USING (principal = 'candidate' AND user_id = app_user_id() AND app_agent_token_id() IS NULL) "
        "WITH CHECK (principal = 'candidate' AND user_id = app_user_id() AND app_agent_token_id() IS NULL)",
    ),
    _policy(
        "org_admin",
        "agent_tokens",
        "TO app_api USING (principal = 'organization' AND app_is_org_admin(organization_id) "
        "AND app_agent_token_id() IS NULL) "
        "WITH CHECK (principal = 'organization' AND app_is_org_admin(organization_id) AND app_agent_token_id() IS NULL)",
    ),
    # 5.5 agent-token scopes on a candidate's data (candidate_own needs no token, so these open only what a scope names)
    _policy(
        "agent_matches",
        "match_runs",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_scope_ok('matches:read'))",
    ),
    _policy(
        "agent_matches",
        "match_decisions",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_scope_ok('matches:read') AND outcome = 'shown')",
    ),
    _policy(
        "agent_explanations",
        "match_evidence",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_scope_ok('explanations:read'))",
    ),
    _policy(
        "agent_explanations",
        "profile_facts",
        "FOR SELECT TO app_api USING (user_id = app_user_id() AND app_scope_ok('explanations:read') "
        "AND confirmed_at IS NOT NULL AND rejected_at IS NULL AND superseded_at IS NULL)",
    ),
    # amendment (c): what an organization token reads. A token has a member (its issuer) and scopes; accepted
    # introductions only, and shortlist entries only while the candidate's consent is live.
    _policy(
        "agent_introductions",
        "introductions",
        "FOR SELECT TO app_api USING (app_agent_token_id() IS NOT NULL AND app_is_org_member(organization_id) "
        "AND status = 'accepted' AND (app_scope_ok('twin:ask') OR app_scope_ok('introductions:request')))",
    ),
    _policy(
        "agent_shortlist",
        "shortlist_entries",
        "FOR SELECT TO app_api USING (app_agent_token_id() IS NOT NULL AND app_is_org_member(organization_id) "
        "AND (app_scope_ok('twin:ask') OR app_scope_ok('introductions:request')) "
        "AND has_active_consent(user_id, 'introduction', organization_id, role_id))",
    ),
    _policy(
        "agent_roles",
        "roles",
        "FOR SELECT TO app_api USING (app_agent_token_id() IS NOT NULL AND app_is_org_member(organization_id) "
        "AND (app_scope_ok('twin:ask') OR app_scope_ok('introductions:request')))",
    ),
    _policy(
        "agent_organizations",
        "organizations",
        "FOR SELECT TO app_api USING (app_agent_token_id() IS NOT NULL AND app_is_org_member(id) "
        "AND (app_scope_ok('twin:ask') OR app_scope_ok('introductions:request')))",
    ),
    _policy(
        "org_token_calls",
        "audit_log",
        "FOR SELECT TO app_api USING (app_agent_token_id() IS NULL AND organization_id IS NOT NULL "
        "AND agent_token_id IS NOT NULL AND app_is_org_admin(organization_id))",
    ),
]

SECURITY = (
    "ALTER TABLE twin_scopes   ENABLE ROW LEVEL SECURITY;\n"
    "ALTER TABLE twin_sessions ENABLE ROW LEVEL SECURITY;\n"
    "ALTER TABLE twin_messages ENABLE ROW LEVEL SECURITY;\n"
    "ALTER TABLE agent_tokens  ENABLE ROW LEVEL SECURITY;\n"
    # the worker serves every person; its privileges below still bind it
    "CREATE POLICY worker_all ON twin_scopes   TO app_worker USING (true) WITH CHECK (true);\n"
    "CREATE POLICY worker_all ON twin_sessions TO app_worker USING (true) WITH CHECK (true);\n"
    "CREATE POLICY worker_all ON twin_messages TO app_worker USING (true) WITH CHECK (true);\n"
    "CREATE POLICY worker_all ON agent_tokens  TO app_worker USING (true) WITH CHECK (true);\n"
    + "\n".join(POLICIES)
    + """
-- 5.7 grants. twin_messages is append-only: insert and read for both logins, never update or delete.
GRANT SELECT, INSERT ON twin_messages TO app_api, app_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON twin_scopes TO app_api, app_worker;
GRANT SELECT, INSERT ON twin_sessions TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON twin_sessions TO app_worker;
GRANT SELECT, INSERT ON agent_tokens TO app_api;
GRANT UPDATE (revoked_at) ON agent_tokens TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_tokens TO app_worker;
"""
)

# --------------------------------------------------------------------------------------------------------------------

DOWNGRADE = """
DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

DROP POLICY IF EXISTS agent_matches ON match_runs;
DROP POLICY IF EXISTS agent_matches ON match_decisions;
DROP POLICY IF EXISTS agent_explanations ON match_evidence;
DROP POLICY IF EXISTS agent_explanations ON profile_facts;
DROP POLICY IF EXISTS agent_introductions ON introductions;
DROP POLICY IF EXISTS agent_shortlist ON shortlist_entries;
DROP POLICY IF EXISTS agent_roles ON roles;
DROP POLICY IF EXISTS agent_organizations ON organizations;
DROP POLICY IF EXISTS org_token_calls ON audit_log;

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_agent_token_id_fkey;
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_twin_session_id_fkey;

-- the tables, with their triggers, indexes, policies, grants and foreign keys
DROP TABLE twin_messages, twin_sessions, agent_tokens, twin_scopes;

DROP FUNCTION twin_questions_delete_thread(), agent_tokens_check(), twin_messages_check(), twin_sessions_check(),
              twin_message_citations(uuid), role_ask_bucket(uuid), touch_agent_token(uuid),
              resolve_agent_token(bytea), app_scope_ok(token_scope);
"""


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    for name, labels in ENUMS.items():
        op.execute(_enum(name, labels))
    op.execute(TABLES)
    op.execute(FUNCTIONS)
    op.execute(GUARD_PHASE_9)
    op.execute(SECURITY)
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(GUARD_PHASE_7)
    op.execute(DOWNGRADE)
    names = ", ".join(reversed(list(ENUMS)))
    op.execute(f"DROP TYPE {names}")
    op.execute("RESET ROLE")
