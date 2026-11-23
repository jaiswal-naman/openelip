"""The migration history builds the schema DATA_MODEL describes, keeps the job rows it adopts, and round-trips."""

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

# What information_schema must show after `alembic upgrade head`, as "column udt_name" ("?" marks a nullable column).
# Transcribed by hand from DATA_MODEL 3.3, 3.7 and 3.13 (the Phase 1 tables) and 3.5 (the job index, converted in
# place by revision p2_0201_job_index), so a change to either side that the other does not follow fails here.
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


def test_the_harness_refuses_databases_that_do_not_end_in_test(assert_test_db):
    for name in ("app", "postgres", "app_test_backup", "app-test", "App_test", ""):
        with pytest.raises(RuntimeError):
            assert_test_db(make_url(f"postgresql://app@localhost/{name}"))
    assert_test_db(make_url("postgresql://app@localhost/app_test"))
