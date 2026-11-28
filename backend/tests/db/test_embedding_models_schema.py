"""Revision e1_embedding_models: jobs.embedding holds vectors of any one width, an index for each model (AI_SYSTEM 3.1).

The column was vector(384), which the local model fills. OpenAI's text-embedding-3-large at 1536 dimensions needs
another width in the same column, and an HNSW index needs a fixed one, so there are two partial expression indexes, one
per model, as pgvector's documentation describes for mixed widths. Most tests run in the `conn` fixture's transaction
on the main test database (at head), which is rolled back; the revision's own up and down run on a scratch database
whose name carries the process id.
"""

import os
from contextlib import contextmanager

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.platform.llm import embed
from tests.db.test_job_index_schema import add_job

REVISION = "e1_embedding_models"
PREVIOUS = "p9_0901_twin_agentnet"
LOCAL_INDEX, OPENAI_INDEX, OLD_INDEX = "jobs_embedding_local_hnsw", "jobs_embedding_openai_hnsw", "jobs_embedding_hnsw"


def literal(first, width):
    return "[" + ",".join([str(first)] + ["0"] * (width - 1)) + "]"


def set_vector(conn, job, first, width, model):
    conn.execute(
        text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
        {"v": literal(first, width), "m": model, "id": job},
    )


def typmod(conn):
    return conn.execute(
        text("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'jobs'::regclass AND attname = 'embedding'")
    ).scalar_one()


def index_definitions(conn):
    rows = conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'"))
    return {r.indexname: r.indexdef for r in rows}


@contextmanager
def scratch(make_database, tag):
    url = make_database(f"app_e1_{tag}_{os.getpid()}_test")
    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        yield url, engine
    finally:
        engine.dispose()


def test_the_revision_sits_on_the_twin_revision_and_is_the_only_head():
    config = Config()
    config.set_main_option("script_location", str(os.path.join(os.path.dirname(__file__), "..", "..", "..", "db")))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS


def test_the_column_has_no_fixed_width_and_each_model_has_its_own_partial_hnsw_index(conn):
    assert typmod(conn) == -1  # vector, not vector(384)
    definitions = index_definitions(conn)

    assert OLD_INDEX not in definitions
    local, openai = definitions[LOCAL_INDEX], definitions[OPENAI_INDEX]
    for definition, width, model in ((local, 384, embed.LOCAL_MODEL_ID), (openai, 1536, embed.OPENAI_MODEL_ID)):
        assert "USING hnsw" in definition and "vector_cosine_ops" in definition
        assert f"(embedding)::vector({width})" in definition
        assert f"embedding_model = '{model}'" in definition


def test_vectors_of_both_widths_live_in_the_one_column_and_each_index_answers_for_its_own_model(conn):
    first, second = add_job(conn, external_id="a"), add_job(conn, external_id="b")
    set_vector(conn, first, 0.5, 384, embed.LOCAL_MODEL_ID)
    set_vector(conn, second, 0.5, 1536, embed.OPENAI_MODEL_ID)

    widths = dict(conn.execute(text("SELECT id, vector_dims(embedding) FROM jobs ORDER BY id")).all())
    assert widths == {first: 384, second: 1536}

    conn.execute(text("SET LOCAL enable_seqscan = off"))  # a table of two rows would otherwise never need an index
    for width, model, index, job in (
        (384, embed.LOCAL_MODEL_ID, LOCAL_INDEX, first),
        (1536, embed.OPENAI_MODEL_ID, OPENAI_INDEX, second),
    ):
        query = (
            f"SELECT id FROM jobs WHERE embedding_model = '{model}' "
            f"ORDER BY embedding::vector({width}) <=> CAST(:q AS vector({width})) LIMIT 5"
        )
        plan = "\n".join(conn.execute(text("EXPLAIN " + query), {"q": literal(1, width)}).scalars())
        assert index in plan  # the form pgvector documents does use the index for its model
        assert conn.execute(text(query), {"q": literal(1, width)}).scalars().all() == [job]


def test_a_row_can_be_a_different_width_than_the_query_only_if_the_model_filter_comes_first(conn):
    """This is the shape of the search and retrieval statements: rows are chosen by model before any distance is taken,
    so a vector of the other width is never compared. A different width compared anyway is an error, not a wrong
    rank."""
    local, openai = add_job(conn, external_id="a"), add_job(conn, external_id="b")
    set_vector(conn, local, 1, 384, embed.LOCAL_MODEL_ID)
    set_vector(conn, openai, 1, 1536, embed.OPENAI_MODEL_ID)

    nearest = conn.execute(
        text(
            """WITH eligible AS MATERIALIZED (SELECT id, embedding, embedding_model FROM jobs)
               SELECT id FROM (SELECT e.id, e.embedding <=> CAST(:v AS vector) AS distance
                                 FROM eligible e WHERE e.embedding IS NOT NULL AND e.embedding_model = :m
                                ORDER BY distance, e.id LIMIT 200) nearest"""
        ),
        {"v": literal(1, 1536), "m": embed.OPENAI_MODEL_ID},
    )
    assert nearest.scalars().all() == [openai]

    with pytest.raises(DBAPIError):
        conn.execute(text("SELECT embedding <=> CAST(:v AS vector) FROM jobs"), {"v": literal(1, 1536)}).all()


def test_the_downgrade_refuses_to_run_on_the_live_database_before_it_changes_anything(conn):
    config = Config()
    config.set_main_option("script_location", str(os.path.join(os.path.dirname(__file__), "..", "..", "..", "db")))
    sql = str(ScriptDirectory.from_config(config).get_revision(REVISION).module.DOWNGRADE_SQL).strip()
    assert sql.startswith("DO $$")  # the guard comes before every DROP
    guard = sql.split("END $$;", 1)[0] + "END $$;"
    conn.execute(text("CREATE SCHEMA live_stand_in"))
    conn.execute(
        text("CREATE FUNCTION live_stand_in.current_database() RETURNS name LANGUAGE sql AS $$ SELECT 'app'::name $$")
    )
    conn.execute(text("SET LOCAL search_path = live_stand_in, pg_catalog, public"))
    savepoint = conn.begin_nested()
    with pytest.raises(DBAPIError) as caught:
        conn.execute(text(guard))
    savepoint.rollback()
    assert caught.value.orig.diag.message_primary == "refusing a lossy downgrade on app"

    conn.execute(text("RESET search_path"))
    conn.execute(text(guard))  # any other database name: raises nothing


def test_up_down_and_up_again_keep_the_narrow_vectors_and_drop_only_the_wide_ones(make_database, alembic_run):
    with scratch(make_database, "round") as (url, engine):
        alembic_run("upgrade", url, PREVIOUS)
        with engine.connect() as c:
            assert typmod(c) == 384  # vector(384) before this revision
            assert OLD_INDEX in index_definitions(c)

        alembic_run("upgrade", url, REVISION)
        with engine.begin() as c:
            narrow, wide = add_job(c, external_id="narrow"), add_job(c, external_id="wide")
            set_vector(c, narrow, 0.5, 384, embed.LOCAL_MODEL_ID)
            set_vector(c, wide, 0.5, 1536, embed.OPENAI_MODEL_ID)

        alembic_run("downgrade", url, PREVIOUS)
        with engine.connect() as c:
            assert typmod(c) == 384
            definitions = index_definitions(c)
            assert OLD_INDEX in definitions and LOCAL_INDEX not in definitions and OPENAI_INDEX not in definitions
            assert "WHERE" in definitions[OLD_INDEX] and "canonical_job_id IS NULL" in definitions[OLD_INDEX]
            kept = {
                r.id: (r.embedding_model, r.width)
                for r in c.execute(text("SELECT id, embedding_model, vector_dims(embedding) AS width FROM jobs"))
            }
            # the 384-wide vector stays; the 1536-wide one is cleared with its model name (the CHECK), to be made again
            assert kept == {narrow: (embed.LOCAL_MODEL_ID, 384), wide: (None, None)}

        alembic_run("upgrade", url, REVISION)
        alembic_run("upgrade", url, REVISION)  # twice: the second changes nothing
        with engine.connect() as c:
            assert typmod(c) == -1
            assert {LOCAL_INDEX, OPENAI_INDEX} <= set(index_definitions(c))
