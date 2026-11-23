-- Revision p6_0601_hiring, downgrade: back to the shape revision p4_0401_match_brief leaves. Read by the revision, which
-- runs it as app_owner.
--
-- It exists so the CI migration check (up, down to 0001_baseline, up) can run on a scratch database. It is lossy by nature:
-- every organization, role, introduction, shortlist entry and signal is dropped. Never run it against a database whose
-- rows matter. eval_suite keeps the role_intake value, because PostgreSQL cannot remove an enum value; the type is
-- dropped only when the first revision goes down.
--
-- The first statement refuses the live database, which is called app. The migration check in CI and the tests use other
-- names (migration_check, names ending in _test).

DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;

-- The foreign keys this revision gave to tables that stay (DATA_MODEL 3.14): the columns go back to plain ones.
ALTER TABLE brief_items DROP CONSTRAINT IF EXISTS brief_items_introduction_id_fkey;
ALTER TABLE match_runs  DROP CONSTRAINT IF EXISTS match_runs_role_id_fkey;
ALTER TABLE job_sources DROP CONSTRAINT IF EXISTS job_sources_organization_id_fkey;
ALTER TABLE audit_log   DROP CONSTRAINT IF EXISTS audit_log_organization_id_fkey;
ALTER TABLE consents    DROP CONSTRAINT IF EXISTS consents_role_id_fkey,
                        DROP CONSTRAINT IF EXISTS consents_organization_id_fkey;

-- The tables, with their triggers, indexes, policies and grants, then the functions that served them and the types.
DROP TABLE interest_signals, shortlist_entries, introductions, role_requirements, roles, org_members, organizations;
DROP FUNCTION IF EXISTS introduction_role(uuid), my_org_invitations(), role_preview(uuid), suppress_small(bigint),
                        suppress_below_five(bigint), org_directory(uuid), accept_org_invitation(uuid), create_organization(text, text),
                        interest_signals_check(), shortlist_entries_check(), introductions_guard(),
                        app_is_org_admin(uuid), app_is_org_member(uuid), organization_has_overdue_answers(uuid),
                        has_active_consent(uuid, consent_purpose, uuid, uuid), consent_is_active(uuid),
                        add_business_days(timestamptz, integer, text);
DROP TYPE signal_side, employer_answer, introduction_status, role_status, member_status, org_role;
