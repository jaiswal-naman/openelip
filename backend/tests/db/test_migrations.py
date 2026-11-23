"""The migration history builds the schema DATA_MODEL describes, keeps the job rows it adopts, and round-trips."""

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

REPO = Path(__file__).resolve().parents[3]

# What information_schema must show after `alembic upgrade head`, as "column udt_name" ("?" marks a nullable column).
# Transcribed by hand from DATA_MODEL 3.3, 3.7 and 3.13 (the Phase 1 tables), 3.3 again for the trust tables that plan
# 03-01 added (consents, audit_log, data_requests), 3.4 for the record tables of plan 03-04 and 3.5 (the job index,
# converted in place by revision p2_0201_job_index), 3.6 and 3.7 for the match and brief tables of plan 04-01
# (outbox is 3.7's and Phase 1's), 3.8 and 3.12 for the tailor, apply and chat tables of plan 05-01 (plus the Phase 5
# column chat_messages.finish_reason), and 3.9 for the hiring tables of plan 06-01, so a change to either side that the
# other does not follow fails here.
# Not autogenerate.
EXPECTED = {
    "users": """
        id uuid; email text; display_name text?; status user_status; is_synthetic bool; timezone text
        brief_frequency brief_frequency; brief_weekday int2?; undo_window_minutes int2; record_version int8
        record_confirmed_at timestamptz?; record_embedding vector?; record_embedding_model text?
        last_active_at timestamptz?; created_at timestamptz; updated_at timestamptz""",
    "sessions": """
        id uuid; user_id uuid; token_sha256 bytea; created_at timestamptz; expires_at timestamptz
        last_seen_at timestamptz?; revoked_at timestamptz?""",
    "login_codes": """
        id uuid; email text; code_hmac bytea; attempts int2; expires_at timestamptz; consumed_at timestamptz?
        created_at timestamptz""",
    "idempotency_keys": """
        user_id uuid; key text; method text; path text; request_sha256 bytea; response_status int2?
        response_body jsonb?; created_at timestamptz""",
    "outbox": """
        id uuid; dedupe_key text; user_id uuid?; template text; payload jsonb; agent_action_id int8?
        status outbox_status; attempts int2; not_before timestamptz; expires_at timestamptz?; locked_at timestamptz?
        provider_message_id text?; last_error_code text?; created_at timestamptz; sent_at timestamptz?""",
    "queue_jobs": """
        id int8; queue text; kind text; payload jsonb; user_id uuid?; unique_key text; status queue_status
        priority int2; run_at timestamptz; attempts int2; max_attempts int2; locked_by text?; locked_at timestamptz?
        lease_seconds int4; last_error_code text?; created_at timestamptz; finished_at timestamptz?""",
    "llm_calls": """
        id int8; created_at timestamptz; task text; prompt_version text; model_id uuid; outcome llm_outcome
        user_id uuid?; organization_id uuid?; campaign_id uuid?; agent_run_id uuid?; eval_run_id uuid?
        input_tokens int4; output_tokens int4; cache_read_tokens int4; cache_write_tokens int4; cost_microusd int8
        latency_ms int4; attempt int2""",
    "model_registry": """
        id uuid; provider text; model_name text; kind model_kind; is_local bool; input_microusd_per_mtok int8?
        output_microusd_per_mtok int8?; embedding_dimensions int4?; retention_note text; is_active bool
        price_checked_at timestamptz?; created_at timestamptz""",
    "eval_runs": """
        id uuid; suite eval_suite; git_sha text; config jsonb; status eval_status; metrics jsonb?
        is_gate_passed bool?; started_at timestamptz; finished_at timestamptz?""",
    "eval_items": """
        id int8; eval_run_id uuid; item_key text; slice text?; source_user_id uuid?; expected jsonb?; actual jsonb?
        is_passed bool; score float4?""",
    "job_sources": """
        id int8; ats ats_type; token text; company_name text; organization_id uuid?; is_active bool
        last_fetched_at timestamptz?; last_status fetch_status?; consecutive_failures int4; created_at timestamptz""",
    "jobs": """
        id int8; source_id int8; external_id text; title text; company_name text; location_raw text?; country bpchar?
        city text?; work_arrangements _work_arrangement; employment_type employment_type; employment_type_raw text?
        seniority seniority; salary_min int4?; salary_max int4?; salary_currency bpchar?
        visa_sponsorship visa_sponsorship; department text?; description text; apply_url text; posted_at timestamptz?
        content_hash text; content_version int4; change_note text?; materially_changed_at timestamptz; dedupe_key text
        canonical_job_id int8?; is_listed bool; status job_status; first_seen_at timestamptz
        last_verified_at timestamptz; closed_at timestamptz?; apply_checked_at timestamptz?; apply_check_ok bool?
        requirements_version int4?; search tsvector?; embedding vector?; embedding_model text?""",
    "fetch_runs": """
        id int8; source_id int8; started_at timestamptz; finished_at timestamptz?; status fetch_status
        http_status int4?; jobs_seen int4; jobs_new int4; jobs_changed int4; jobs_closed int4; error text?""",
    "skills": """
        id int4; external_id text; name text; category text?; aliases _text; taxonomy_version text""",
    "job_requirements": """
        id int8; job_id int8; content_version int4; ordinal int2; importance requirement_importance
        kind requirement_kind; text text; span int4range; skill_ids _int4; min_years int2?; prompt_version text
        llm_call_id int8?; created_at timestamptz""",
    "consents": """
        id uuid; user_id uuid; purpose consent_purpose; action consent_action; withdraws_id uuid?
        organization_id uuid?; role_id uuid?; text_version text; created_at timestamptz""",
    "audit_log": """
        id int8; created_at timestamptz; event_type text; actor_type actor_type; actor_user_id uuid?
        subject_user_id uuid?; organization_id uuid?; agent_token_id uuid?; entity_table text?; entity_id text?
        details jsonb""",
    "data_requests": """
        id uuid; user_id uuid?; subject_user_id uuid; kind data_request_kind; status data_request_status
        due_at timestamptz; export_archive bytea?; export_expires_at timestamptz?; store_results jsonb
        created_at timestamptz; completed_at timestamptz?""",
    # DATA_MODEL 3.4, the Career Record tables that plan 03-04 added.
    "documents": """
        id uuid; user_id uuid; content bytea; original_filename text?; mime_type text; byte_size int4; sha256 bytea
        status document_status; extracted_text text?; hidden_text_chars int4; parse_error_code text?
        created_at timestamptz; parsed_at timestamptz?""",
    "profile_events": """
        id uuid; user_id uuid; event_type profile_event_type; entity profile_entity; operation profile_operation
        entity_id uuid?; before jsonb?; after jsonb?; source record_source?; origin proposal_origin?; origin_ref text?
        proposal_event_id uuid?; reason text?; record_version_after int8?; created_at timestamptz""",
    "profile_facts": """
        id uuid; user_id uuid; kind fact_kind; statement text; attributes jsonb; skill_id int4?; parent_fact_id uuid?
        source record_source; document_id uuid?; source_span int4range?; source_quote text?; profile_event_id uuid?
        created_at timestamptz; confirmed_at timestamptz?; rejected_at timestamptz?; superseded_at timestamptz?
        superseded_by uuid?""",
    "constraints": """
        id uuid; user_id uuid; kind constraint_kind; statement text; country_codes _bpchar?; cities _text?
        arrangements _work_arrangement?; employment_types _employment_type?; seniorities _seniority?
        salary_floor int4?; salary_currency bpchar?; company_names _text?; source record_source
        profile_event_id uuid; created_at timestamptz; superseded_at timestamptz?; superseded_by uuid?""",
    "preferences": """
        id uuid; user_id uuid; kind preference_kind; value text; skill_id int4?; weight int2; statement text
        source record_source; profile_event_id uuid; created_at timestamptz; superseded_at timestamptz?
        superseded_by uuid?""",
    # DATA_MODEL 3.6 and 3.7, the match and brief tables that plan 04-01 added.
    "match_runs": """
        id uuid; trigger match_trigger; user_id uuid?; role_id uuid?; local_date date; status match_run_status
        record_version int8?; since timestamptz?; ranker_version text; evidence_prompt_version text?
        retrieved_count int4; filtered_count int4; shown_count int2; error_code text?; started_at timestamptz
        finished_at timestamptz?""",
    "match_decisions": """
        id uuid; match_run_id uuid; user_id uuid; job_id int8; job_content_version int4; local_date date
        outcome match_outcome; reason_codes _match_reason; violated_kinds _constraint_kind
        unverified _unverified_flag; retrieval_rank int4?; rrf_score float4?; fit_score float4?; rank_position int2?
        summary text?; change_note text?; created_at timestamptz""",
    "match_evidence": """
        id int8; match_decision_id uuid; user_id uuid; job_requirement_id int8; verdict evidence_verdict
        fact_ids _uuid; note text?; llm_call_id int8?; created_at timestamptz""",
    "feedback_events": """
        id uuid; user_id uuid; job_id int8; match_decision_id uuid?; kind feedback_kind
        dismiss_reason dismiss_reason?; comment text?; created_at timestamptz""",
    "briefs": """
        id uuid; user_id uuid; local_date date; channel brief_channel; status brief_status; match_run_id uuid?
        covers_since timestamptz?; send_after timestamptz?; sent_at timestamptz?; created_at timestamptz""",
    "brief_items": """
        brief_id uuid; position int2; user_id uuid; kind brief_item_kind; match_decision_id uuid?; approval_id uuid?
        agent_action_id int8?; introduction_id uuid?""",
    # DATA_MODEL 3.9, the hiring tables that plan 06-01 added. name_key is a generated column, so it is nullable here.
    "organizations": """
        id uuid; name text; name_key text?; timezone text; is_synthetic bool; created_by uuid?
        created_at timestamptz""",
    "org_members": """
        id uuid; organization_id uuid; user_id uuid?; invited_email text?; role org_role; status member_status
        invited_by uuid?; created_at timestamptz; joined_at timestamptz?; removed_at timestamptz?""",
    "roles": """
        id uuid; organization_id uuid; title text; description text; status role_status; country bpchar?; city text?
        work_arrangements _work_arrangement; employment_type employment_type; seniority seniority; salary_min int4?
        salary_max int4?; salary_currency bpchar?; visa_sponsorship visa_sponsorship; job_id int8?
        requirements_confirmed_at timestamptz?; created_by uuid?; created_at timestamptz; opened_at timestamptz?
        closed_at timestamptz?""",
    "role_requirements": """
        id uuid; role_id uuid; organization_id uuid; ordinal int2; importance requirement_importance
        kind requirement_kind; text text; span int4range?; is_confirmed bool; created_at timestamptz""",
    "introductions": """
        id uuid; organization_id uuid; role_id uuid; user_id uuid; match_decision_id uuid; status introduction_status
        share_preview jsonb; asked_at timestamptz; expires_at timestamptz; responded_at timestamptz?
        answer_due_at timestamptz?; employer_answer employer_answer?; employer_answer_reason text?
        employer_answered_by uuid?; employer_answered_at timestamptz?; created_at timestamptz""",
    "shortlist_entries": """
        id uuid; organization_id uuid; role_id uuid; introduction_id uuid; user_id uuid; shared_payload jsonb
        contact_released_at timestamptz?; created_at timestamptz""",
    "interest_signals": """
        id uuid; sender_side signal_side; user_id uuid; organization_id uuid; role_id uuid; introduction_id uuid?
        sent_by uuid?; week_start date; slot int2; created_at timestamptz""",
    # DATA_MODEL 3.8 and 3.12, the tailor, apply and chat tables that plan 05-01 added.
    "tailored_resumes": """
        id uuid; user_id uuid; job_id int8; job_content_version int4; base_document_id uuid; record_version int8
        status tailored_status; prompt_version text?; discarded_count int4; created_at timestamptz""",
    "resume_edits": """
        id uuid; tailored_resume_id uuid; user_id uuid; ordinal int2; section text; target_fact_id uuid?
        before_text text; after_text text; supporting_fact_ids _uuid; verifier_status edit_verifier_status
        discard_reason edit_discard_reason?; decision edit_decision; decided_at timestamptz?; created_at timestamptz""",
    "application_packets": """
        id uuid; user_id uuid; job_id int8; campaign_id uuid?; tailored_resume_id uuid?; answers jsonb
        cover_note text?; status packet_status; approved_at timestamptz?; created_at timestamptz""",
    "applications": """
        id uuid; user_id uuid; job_id int8; campaign_id uuid?; application_packet_id uuid?
        status application_status; status_changed_at timestamptz; created_at timestamptz""",
    "application_events": """
        id int8; application_id uuid; user_id uuid; status application_status; occurred_at timestamptz
        created_at timestamptz""",
    "chat_sessions": """
        id uuid; user_id uuid; kind chat_session_kind; created_at timestamptz; last_message_at timestamptz?""",
    "chat_messages": """
        id uuid; session_id uuid; user_id uuid; role chat_role; content text?; tool_name text?; refs jsonb
        llm_call_id int8?; finish_reason text?; created_at timestamptz""",
}


def _parse(spec: str) -> set[tuple[str, str, bool]]:
    entries = (item.strip() for line in spec.splitlines() for item in line.split(";"))
    columns = set()
    for entry in filter(None, entries):
        name, udt = entry.split()
        columns.add((name, udt.rstrip("?"), udt.endswith("?")))
    return columns


def _actual_columns(engine: Engine) -> dict[str, set[tuple[str, str, bool]]]:
    rows = engine.connect().execute(
        text(
            "SELECT table_name, column_name, udt_name, is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
        )
    )
    tables: dict[str, set[tuple[str, str, bool]]] = {}
    for table, column, udt, nullable in rows:
        tables.setdefault(table, set()).add((column, udt, nullable))
    return tables


def _assert_matches_data_model(engine: Engine) -> None:
    actual = _actual_columns(engine)
    assert set(actual) == set(EXPECTED)
    for table, spec in EXPECTED.items():
        assert actual[table] == _parse(spec), table


def _engine(url) -> Engine:
    return create_engine(url.set(drivername="postgresql+psycopg"))


def test_upgrade_from_empty_builds_every_phase_1_table_and_the_job_tables(make_database, alembic_run):
    url = make_database("app_migrations_test")
    alembic_run("upgrade", url, "head")
    _assert_matches_data_model(_engine(url))


def test_downgrade_to_the_baseline_then_upgrade_again(make_database, alembic_run):
    url = make_database("app_roundtrip_test")
    alembic_run("upgrade", url, "head")
    alembic_run("downgrade", url, "0001_baseline")
    engine = _engine(url)
    assert set(_actual_columns(engine)) == {"job_sources", "jobs", "fetch_runs"}
    owners = (
        engine.connect()
        .execute(
            text("SELECT DISTINCT tableowner FROM pg_tables WHERE tablename = ANY(:t)"),
            {"t": ["job_sources", "jobs", "fetch_runs"]},
        )
        .scalars()
        .all()
    )
    assert owners == ["app"]  # ownership went back to the bootstrap login
    alembic_run("upgrade", url, "head")
    _assert_matches_data_model(engine)


def test_the_job_rows_survive_the_upgrade(make_database, alembic_run):
    """The live-database adoption in miniature: baseline, some rows, then every later revision."""
    url = make_database("app_adopt_test")
    alembic_run("upgrade", url, "0001_baseline")
    engine = _engine(url)
    with engine.begin() as c:
        source = c.execute(
            text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
        ).scalar_one()
        for n in range(3):
            c.execute(
                text(
                    "INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, "
                    "content_hash, dedupe_key) VALUES (:s, :e, 'Engineer', 'Acme', 'Build things', "
                    "'https://example.test/' || :e, 'h', 'd')"
                ),
                {"s": source, "e": f"job-{n}"},
            )
        c.execute(text("INSERT INTO fetch_runs (source_id, status) VALUES (:s, 'ok')"), {"s": source})

    count_rows = text(
        "SELECT (SELECT count(*) FROM job_sources), (SELECT count(*) FROM jobs), (SELECT count(*) FROM fetch_runs)"
    )
    with engine.connect() as c:
        before = tuple(c.execute(count_rows).one())
    alembic_run("upgrade", url, "head")
    with engine.connect() as c:
        after = tuple(c.execute(count_rows).one())
        searchable = c.execute(
            text("SELECT count(*) FROM jobs WHERE search @@ websearch_to_tsquery('english', 'engineer')")
        ).scalar_one()
        owners = (
            c.execute(
                text("SELECT DISTINCT tableowner FROM pg_tables WHERE tablename = ANY(:t)"),
                {"t": ["job_sources", "jobs", "fetch_runs"]},
            )
            .scalars()
            .all()
        )
    assert before == after == (1, 3, 1)
    assert searchable == 3
    assert owners == ["app_owner"]


# ---- revision p5_0501_chat_tailor_apply (plan 05-01) ----------------------------------------------------------------

REVISION = "p5_0501_chat_tailor_apply"
PREVIOUS = "p4_0401_match_brief"
NEW_TABLES = (
    "tailored_resumes",
    "resume_edits",
    "application_packets",
    "applications",
    "application_events",
    "chat_sessions",
    "chat_messages",
)
NEW_ENUMS = (
    "tailored_status",
    "edit_verifier_status",
    "edit_discard_reason",
    "edit_decision",
    "application_status",
    "packet_status",
    "chat_session_kind",
    "chat_role",
)
NEW_FUNCTIONS = ("resume_edits_check_support", "resume_edits_decide_once")


def _names(connection, sql: str, **params) -> list[str]:
    return sorted(connection.execute(text(sql), params).scalars())


def test_the_phase_5_revision_sits_on_the_phase_4_one_and_the_merge_with_phase_6_is_the_only_head():
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PREVIOUS
    assert script.get_heads() == ["p56_merge_chat_hiring"]
    assert set(script.get_revision("p56_merge_chat_hiring").down_revision) == {REVISION, "p6_0601_hiring"}


def _data_model_labels(type_name: str) -> list[str]:
    source = (REPO / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")
    found = re.search(rf"CREATE TYPE\s+{type_name}\s+AS ENUM\s*\(([^)]*)\)", source)
    assert found, f"DATA_MODEL.md has no CREATE TYPE {type_name}"
    return re.findall(r"'([^']+)'", found.group(1))


@pytest.mark.parametrize("type_name", NEW_ENUMS)
def test_every_phase_5_enum_holds_the_data_model_values_in_order(engine, type_name):
    with engine.connect() as c:
        labels = c.execute(
            text("SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:t AS regtype) ORDER BY enumsortorder"),
            {"t": type_name},
        ).scalars()
        assert list(labels) == _data_model_labels(type_name)


def test_the_phase_5_revision_goes_down_one_step_leaving_nothing_and_up_again_twice_changes_nothing(
    make_database, alembic_run
):
    url = make_database("p5down_test")
    engine = _engine(url)
    try:
        alembic_run("upgrade", url, REVISION)
        alembic_run("downgrade", url, PREVIOUS)
        with engine.connect() as c:
            left = {
                "tables": _names(c, "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES)),
                "types": _names(c, "SELECT typname FROM pg_type WHERE typname = ANY(:n)", n=list(NEW_ENUMS)),
                "functions": _names(c, "SELECT proname FROM pg_proc WHERE proname = ANY(:n)", n=list(NEW_FUNCTIONS)),
                "policies": _names(
                    c, "SELECT policyname FROM pg_policies WHERE tablename = ANY(:n)", n=list(NEW_TABLES)
                ),
            }
            assert left == {name: [] for name in left}
            assert _names(c, "SELECT version_num FROM alembic_version") == [PREVIOUS]
        alembic_run("upgrade", url, REVISION)
        alembic_run("upgrade", url, REVISION)  # already there: a second upgrade changes nothing
        with engine.connect() as c:
            found = _names(c, "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES))
            assert found == sorted(NEW_TABLES)
            assert _names(c, "SELECT version_num FROM alembic_version") == [REVISION]
    finally:
        engine.dispose()


def test_the_harness_refuses_databases_that_do_not_end_in_test(assert_test_db):
    for name in ("app", "postgres", "app_test_backup", "app-test", "App_test", ""):
        with pytest.raises(RuntimeError):
            assert_test_db(make_url(f"postgresql://app@localhost/{name}"))
    assert_test_db(make_url("postgresql://app@localhost/app_test"))
