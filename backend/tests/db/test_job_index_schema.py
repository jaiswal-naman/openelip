"""Revision p2_0201_job_index: the typed job index, the requirement table and the guards that keep its spans true.

Most tests run in the `conn` fixture's transaction on the main test database (already at head), which is rolled back.
The ones that need a database at another revision get a scratch database of their own. Its name carries the process
id, so two runs at once never drop each other's database.
"""

import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError

REVISION = "p2_0201_job_index"
PHASE_1_HEAD = "0002_platform"
EMBEDDING_MODEL = "test-embedding-model"
DOWNGRADE_SQL = Path(__file__).resolve().parents[3] / "db" / "sql" / "p2_0201_downgrade.sql"

# The columns DATA_MODEL 3.5 adds to jobs (it also removes is_remote).
NEW_JOB_COLUMNS = {
    "work_arrangements", "employment_type", "employment_type_raw", "seniority", "salary_min", "salary_max",
    "salary_currency", "visa_sponsorship", "change_note", "materially_changed_at", "is_listed", "apply_checked_at",
    "apply_check_ok", "requirements_version", "embedding_model",
}  # fmt: skip

# A job as Phase 1 stored it: is_remote and a free-text employment_type. The vector is only on the silent one.
INSERT_OLD_JOB = text(
    """INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, content_hash, dedupe_key,
                        is_remote, employment_type, status, last_verified_at, first_seen_at, embedding)
       VALUES (1, :e, 'Engineer', 'Acme', 'Build things', 'https://example.test/' || :e, 'h', 'd', :r, :w, :s, :v,
               '2026-02-01 00:00+00', CASE WHEN :e = 'silent' THEN array_fill(0.1::real, ARRAY[384])::vector END)"""
)
# A job in the new shape, with the arrangements and the wording the board used.
INSERT_NEW_JOB = text(
    """INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, content_hash, dedupe_key,
                       work_arrangements, employment_type_raw)
       VALUES (1, :e, 'Engineer', 'Acme', 'Build things', 'https://example.test/' || :e, 'h', 'd',
               CAST(:a AS work_arrangement[]), :w)"""
)


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def sqlstate(caught) -> str:
    return caught.value.orig.sqlstate


def message(caught) -> str:
    return caught.value.orig.diag.message_primary


def names(c: Connection, sql: str) -> set[str]:
    return set(c.execute(text(sql)).scalars())


def add_job(conn: Connection, description: str = "Build and run things.", external_id: str = "job-1") -> int:
    source = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme')
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name RETURNING id"""
        )
    ).scalar_one()
    return conn.execute(
        text(
            """INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, content_hash,
                                dedupe_key)
               VALUES (:s, :e, 'Engineer', 'Acme', :d, 'https://example.test/apply', 'h', 'd') RETURNING id"""
        ),
        {"s": source, "e": external_id, "d": description},
    ).scalar_one()


def add_requirement(conn: Connection, job_id: int, quote: str, start: int, end: int, version: int = 1) -> None:
    conn.execute(
        text(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                             prompt_version)
               VALUES (:j, :v, 1, 'required', 'skill', :q, int4range(:s, :e), 'test-v1')"""
        ),
        {"j": job_id, "v": version, "q": quote, "s": start, "e": end},
    )


def add_skill(conn: Connection) -> None:
    conn.execute(text("INSERT INTO skills (id, external_id, name, taxonomy_version) VALUES (1, 'x', 'Python', 'test')"))


@contextmanager
def scratch_engine(make_database, tag: str):
    url = make_database(f"app_p2_{tag}_{os.getpid()}_test")
    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        yield url, engine
    finally:
        engine.dispose()


def test_an_empty_database_reaches_the_revision_with_the_typed_columns_and_both_indexes(make_database, alembic_run):
    with scratch_engine(make_database, "empty") as (url, engine):
        alembic_run("upgrade", url, REVISION)
        with engine.connect() as c:
            version = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            columns = names(c, "SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs'")
            tables = names(
                c, "SELECT table_name FROM information_schema.tables WHERE table_name IN ('skills', 'job_requirements')"
            )
            indexes = names(c, "SELECT indexname FROM pg_indexes WHERE tablename = 'jobs'")
    assert version == REVISION
    assert NEW_JOB_COLUMNS <= columns and "is_remote" not in columns
    assert tables == {"skills", "job_requirements"}
    assert {"jobs_embedding_hnsw", "jobs_changed_idx"} <= indexes


def test_rows_in_the_old_shape_survive_and_are_converted_by_the_data_model_rule(
    make_database, alembic_run, monkeypatch
):
    monkeypatch.setenv("EMBEDDING_MODEL", EMBEDDING_MODEL)
    with scratch_engine(make_database, "oldshape") as (url, engine):
        alembic_run("upgrade", url, PHASE_1_HEAD)
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO job_sources (ats, token, company_name, last_status) VALUES ('lever', 'a', 'A', 'ok')")
            )
            for external_id, remote, wording, status, verified in [
                ("remote", True, "Full-time", "open", "2026-03-01 10:00+00"),
                ("not-remote", False, "Contractor", "open", "2026-03-02 10:00+00"),
                ("closed", None, "Seasonal", "closed", "2026-01-02 03:04:05+00"),  # closed, and never said when
                ("silent", None, None, "open", "2026-03-04 10:00+00"),
            ]:
                params = {"e": external_id, "r": remote, "w": wording, "s": status, "v": verified}
                c.execute(INSERT_OLD_JOB, params)
            c.execute(text("INSERT INTO fetch_runs (source_id, status) VALUES (1, 'ok')"))

        alembic_run("upgrade", url, REVISION)

        with engine.connect() as c:
            counts = tuple(
                c.execute(
                    text(
                        """SELECT (SELECT count(*) FROM job_sources), (SELECT count(*) FROM jobs),
                                  (SELECT count(*) FROM fetch_runs)"""
                    )
                ).one()
            )
            rows = {
                r.external_id: tuple(r)[1:]
                for r in c.execute(
                    text(
                        """SELECT external_id, work_arrangements::text[], employment_type::text, employment_type_raw,
                                  status::text, closed_at, materially_changed_at = first_seen_at, embedding_model
                             FROM jobs"""
                    )
                )
            }
            source = tuple(
                c.execute(text("SELECT ats::text, last_status::text, consecutive_failures FROM job_sources")).one()
            )
    assert counts == (1, 4, 1)
    assert source == ("lever", "ok", 0)
    closed_at = rows["closed"][4]
    assert rows == {
        "remote": (["remote"], "full_time", "Full-time", "open", None, True, None),
        "not-remote": (["onsite", "hybrid"], "contract", "Contractor", "open", None, True, None),
        "closed": (
            [],
            "unknown",
            "Seasonal",
            "closed",
            closed_at,
            True,
            None,
        ),  # the wording is kept, the enum says unknown
        "silent": ([], "unknown", None, "open", None, True, EMBEDDING_MODEL),  # the vector is labelled with its model
    }
    assert closed_at.isoformat().startswith("2026-01-02T03:04:05")  # it got its last verified time


def test_a_requirement_must_quote_the_description_at_its_span_counted_in_code_points(conn):
    description = "Intro \U0001f600 emoji, then: 5 years of Python and SQL required."
    start = description.index("5 years")  # a Python index: one position per code point, the emoji counts once
    end = start + len("5 years of Python and SQL")
    job = add_job(conn, description)

    add_requirement(conn, job, description[start:end], start, end)  # the true quote is accepted
    assert conn.execute(text("SELECT count(*) FROM job_requirements")).scalar_one() == 1

    with refused(conn) as caught:  # one character short of the span
        add_requirement(conn, job, description[start : end - 1], start, end)
    assert sqlstate(caught) == "23514" and message(caught).startswith("span_unresolved")

    with refused(conn) as caught:  # right text, but for a version of the job that does not exist
        add_requirement(conn, job, description[start:end], start, end, version=2)
    assert sqlstate(caught) == "23514" and message(caught).startswith("span_unresolved")


def test_job_text_cannot_change_without_a_new_content_version(conn):
    job = add_job(conn)

    with refused(conn) as caught:
        conn.execute(text("UPDATE jobs SET description = 'Different text.' WHERE id = :j"), {"j": job})
    assert sqlstate(caught) == "23514"
    with refused(conn) as caught:  # the title is job text too
        conn.execute(text("UPDATE jobs SET title = 'Other' WHERE id = :j"), {"j": job})
    assert sqlstate(caught) == "23514"

    conn.execute(
        text("UPDATE jobs SET description = 'Different text.', content_version = content_version + 1 WHERE id = :j"),
        {"j": job},
    )
    assert conn.execute(text("SELECT content_version FROM jobs WHERE id = :j"), {"j": job}).scalar_one() == 2
    conn.execute(text("UPDATE jobs SET last_verified_at = now() WHERE id = :j"), {"j": job})  # other columns stay free

    with refused(conn) as caught:
        conn.execute(text("UPDATE jobs SET content_version = 1 WHERE id = :j"), {"j": job})
    assert sqlstate(caught) == "23514"


def test_the_fetch_history_and_the_requirements_accept_no_update_or_delete(conn):
    job = add_job(conn)
    add_requirement(conn, job, "Build", 0, 5)
    conn.execute(
        text("INSERT INTO fetch_runs (source_id, status) SELECT source_id, 'ok' FROM jobs WHERE id = :j"), {"j": job}
    )

    for table, change in [
        ("fetch_runs", "UPDATE fetch_runs SET jobs_seen = 9"),
        ("fetch_runs", "DELETE FROM fetch_runs"),
        ("job_requirements", "UPDATE job_requirements SET ordinal = 2"),
        ("job_requirements", "DELETE FROM job_requirements"),
    ]:
        with refused(conn) as caught:
            conn.execute(text(change))
        assert sqlstate(caught) == "42501", table


def test_the_api_only_reads_the_requirement_tables_and_the_worker_may_write_them(conn):
    job = add_job(conn)

    conn.execute(text("SET LOCAL ROLE app_api"))
    assert conn.execute(text("SELECT count(*) FROM job_requirements")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM skills")).scalar_one() == 0
    with refused(conn) as caught:
        add_requirement(conn, job, "Build", 0, 5)
    assert sqlstate(caught) == "42501"
    with refused(conn) as caught:
        add_skill(conn)
    assert sqlstate(caught) == "42501"

    conn.execute(text("RESET ROLE"))
    conn.execute(text("SET LOCAL ROLE app_worker"))
    add_requirement(conn, job, "Build", 0, 5)
    add_skill(conn)
    conn.execute(text("UPDATE jobs SET last_verified_at = now() WHERE id = :j"), {"j": job})
    with refused(conn) as caught:  # a requirement is written once
        conn.execute(text("UPDATE job_requirements SET ordinal = 2"))
    assert sqlstate(caught) == "42501"


def test_the_eval_suite_accepts_the_suites_of_this_phase(conn):
    for suite in ("location_accuracy", "requirement_extraction"):
        conn.execute(
            text("INSERT INTO eval_runs (suite, git_sha) VALUES (CAST(:s AS eval_suite), 'abc')"), {"s": suite}
        )
    assert conn.execute(text("SELECT count(*) FROM eval_runs")).scalar_one() == 2


def test_the_downgrade_refuses_to_run_on_the_live_database_before_it_changes_anything(conn):
    """The downgrade drops the requirement table and collapses work_arrangements, so its first statement refuses the
    live database, which is called app. A database of that name must not be made here (the real one may be on this
    server), so current_database() is stood in for by a function of that name first on the search path."""
    lines = DOWNGRADE_SQL.read_text(encoding="utf-8").splitlines()
    code = "\n".join(line for line in lines if not line.startswith("--")).strip()
    assert code.startswith("DO $$")  # the guard comes before every DROP
    guard = code.split("END $$;", 1)[0] + "END $$;"

    conn.execute(text("CREATE SCHEMA live_stand_in"))
    conn.execute(
        text("CREATE FUNCTION live_stand_in.current_database() RETURNS name LANGUAGE sql AS $$ SELECT 'app'::name $$")
    )
    conn.execute(text("SET LOCAL search_path = live_stand_in, pg_catalog, public"))
    with refused(conn) as caught:
        conn.execute(text(guard))
    assert message(caught) == "refusing a lossy downgrade on app"

    conn.execute(text("RESET search_path"))  # a scratch database, as the CI migration check and every test use
    conn.execute(text(guard))  # raises nothing
    still_there = conn.execute(text("SELECT to_regclass('job_requirements')")).scalar_one()
    assert still_there is not None


def test_the_downgrade_puts_the_phase_1_shape_back_with_every_row(make_database, alembic_run):
    with scratch_engine(make_database, "downgrade") as (url, engine):
        alembic_run("upgrade", url, REVISION)
        with engine.begin() as c:
            c.execute(text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'a', 'A')"))
            for external_id, arrangements, wording in [
                ("remote", "{remote}", "Full-time"),
                ("not-remote", "{onsite,hybrid}", None),
                ("silent", "{}", "Seasonal"),
            ]:
                c.execute(INSERT_NEW_JOB, {"e": external_id, "a": arrangements, "w": wording})

        alembic_run("downgrade", url, PHASE_1_HEAD)
        with engine.connect() as c:
            old = {
                r.external_id: (r.is_remote, r.employment_type)
                for r in c.execute(text("SELECT external_id, is_remote, employment_type FROM jobs"))
            }
            requirement_table = c.execute(text("SELECT to_regclass('job_requirements')")).scalar_one()
        assert old == {"remote": (True, "Full-time"), "not-remote": (False, None), "silent": (None, "Seasonal")}
        assert requirement_table is None

        alembic_run("upgrade", url, REVISION)
        with engine.connect() as c:
            new = dict(c.execute(text("SELECT external_id, work_arrangements::text[] FROM jobs")).all())
        assert new == {"remote": ["remote"], "not-remote": ["onsite", "hybrid"], "silent": []}
