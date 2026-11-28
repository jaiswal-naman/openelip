"""IDX-04: every open, canonical, listed job gets one vector per content version, from a sweep that runs on a schedule.

The sweep reads the rows that still have no vector, embeds them, and writes each vector only if the row is still the
version it read. The local model is replaced by a stand-in here (the real one is covered in test_embed.py and in
test_search.py), so these tests are about which rows are chosen, what text is embedded and how it is written.
Everything runs in the `conn` fixture's transaction, which is rolled back, except the last test, which uses the
worker's own login.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.ingest import embedding, jobs
from app.platform import config, db, queue, scheduler
from app.platform.llm import embed

NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
FIVE_MINUTES = 300


def vector(first, dimensions=384):
    """A vector of the local model's width (or the width given) that starts with `first` and is zero elsewhere."""
    return [first] + [0.0] * (dimensions - 1)


class StandIn:
    """Replaces the local model: remembers every batch it was given and returns a recognisable vector per text."""

    def __init__(self, dimensions=384):
        self.batches = []
        self.options = []  # what each call said besides the texts: the model it expects
        self.dimensions = dimensions
        self.during = None  # a function to run while the model is "working", to change the database under the sweep

    def __call__(self, texts, **options):
        self.batches.append(list(texts))
        self.options.append(options)
        if self.during is not None:
            self.during()
        return [vector(len(t) / 10000, self.dimensions) for t in texts]

    @property
    def texts(self):
        return [t for batch in self.batches for t in batch]


@pytest.fixture
def model(monkeypatch):
    stand_in = StandIn()
    monkeypatch.setattr(embedding, "embed_passages", stand_in)
    return stand_in


@pytest.fixture
def openai_model(monkeypatch):
    """The platform runs on an OpenAI key, so the active model is OpenAI's, whose vectors are 1536 wide."""
    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"})
    monkeypatch.setattr(config, "get", lambda: settings)
    stand_in = StandIn(dimensions=1536)
    monkeypatch.setattr(embedding, "embed_passages", stand_in)
    return stand_in


def add_source(conn):
    return conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
    ).scalar_one()


def add_job(conn, source, n, *, description=None, title=None, **over):
    fields = {
        "source_id": source,
        "external_id": f"job-{n}",
        "title": title or f"Invented title {n}",
        "company_name": "Acme",
        "description": description or f"Invented description {n}, long enough to be a posting.",
        "apply_url": "https://example.test/apply",
        "content_hash": f"hash-{n}",
        "dedupe_key": f"key-{n}",
        **over,
    }
    names = ", ".join(fields)
    values = ", ".join(f":{name}" for name in fields)
    return conn.execute(text(f"INSERT INTO jobs ({names}) VALUES ({values}) RETURNING id"), fields).scalar_one()


def add_requirement(conn, job_id, ordinal, importance, quote, *, version=1):
    """A requirement row: `quote` must be in the job's description, because the database checks the span."""
    description = conn.execute(text("SELECT description FROM jobs WHERE id = :id"), {"id": job_id}).scalar_one()
    start = description.index(quote)
    conn.execute(
        text(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                             prompt_version)
               VALUES (:job, :version, :ordinal, CAST(:importance AS requirement_importance), 'skill', :quote,
                       int4range(:start, :end), 'invented-test')"""
        ),
        {
            "job": job_id,
            "version": version,
            "ordinal": ordinal,
            "importance": importance,
            "quote": quote,
            "start": start,
            "end": start + len(quote),
        },
    )


def row(conn, job_id):
    return conn.execute(
        text("SELECT embedding::text AS embedding, embedding_model FROM jobs WHERE id = :id"), {"id": job_id}
    ).one()


def stored_first_value(conn, job_id):
    return float(row(conn, job_id).embedding.strip("[]").split(",")[0])


def test_the_text_that_is_embedded_is_the_title_then_what_the_posting_asks_for():
    assert embedding.embedding_text("Backend Engineer", "A long description.", ["Python", "SQL"]) == (
        "Backend Engineer\nPython\nSQL"
    )


def test_without_requirements_it_is_the_title_then_the_first_2000_characters_of_the_description():
    description = "x" * 1990 + "ABCDEFGHIJ" + "y" * 3000

    embedded = embedding.embedding_text("Backend Engineer", description, [])

    assert embedded == "Backend Engineer\n" + "x" * 1990 + "ABCDEFGHIJ"
    assert embedding.embedding_text("Short", "Few words.", []) == "Short\nFew words."


def test_a_vector_is_written_in_pgvector_text_form():
    assert embedding.vector_literal([0.5, -1.0, 0.25]) == "[0.5,-1.0,0.25]"


def test_the_sweep_embeds_open_canonical_listed_jobs_and_leaves_every_other_row_alone(conn, model):
    source = add_source(conn)
    wanted = add_job(conn, source, 1)
    closed = add_job(conn, source, 2, status="closed", closed_at=NOW)
    duplicate = add_job(conn, source, 3, canonical_job_id=wanted)
    unlisted = add_job(conn, source, 4, is_listed=False)
    done = add_job(conn, source, 5, embedding="[" + ",".join(["0.5"] * 384) + "]", embedding_model=embed.LOCAL_MODEL_ID)

    assert embedding.embed_pending(conn) == 1

    assert row(conn, wanted).embedding_model == embed.LOCAL_MODEL_ID  # the vector says which model made it
    assert stored_first_value(conn, wanted) > 0
    for left_alone in (closed, duplicate, unlisted):
        assert tuple(row(conn, left_alone)) == (None, None)
    assert tuple(row(conn, done))[1] == embed.LOCAL_MODEL_ID  # a vector of the active model is not replaced
    assert stored_first_value(conn, done) == 0.5
    assert len(model.texts) == 1


def test_requirements_come_required_first_and_only_for_the_current_version(conn, model):
    source = add_source(conn)
    description = "Invented posting. We prefer Rust. You need Python. You also need SQL."
    with_requirements = add_job(conn, source, 1, title="Backend Engineer", description=description)
    add_requirement(conn, with_requirements, 1, "preferred", "We prefer Rust")
    add_requirement(conn, with_requirements, 2, "required", "You need Python")
    add_requirement(conn, with_requirements, 3, "required", "You also need SQL")
    conn.execute(text("UPDATE jobs SET requirements_version = 1 WHERE id = :id"), {"id": with_requirements})

    old_text = "Invented posting. You need Go."
    stale = add_job(conn, source, 2, title="Platform Engineer", description=old_text)
    add_requirement(conn, stale, 1, "required", "You need Go")
    conn.execute(
        text("UPDATE jobs SET description = :d, content_version = 2, requirements_version = 1 WHERE id = :id"),
        {"d": "Invented posting, rewritten. You need Java.", "id": stale},
    )
    add_job(conn, source, 3, title="Data Engineer", description="Invented posting with no requirements yet.")
    unmarked = add_job(conn, source, 4, title="Site Engineer", description="Invented posting. You need Linux.")
    add_requirement(conn, unmarked, 1, "required", "You need Linux")  # rows, but requirements_version is still NULL

    assert embedding.embed_pending(conn) == 4

    assert sorted(model.texts) == sorted(
        [
            "Backend Engineer\nYou need Python\nYou also need SQL\nWe prefer Rust",
            "Platform Engineer\nInvented posting, rewritten. You need Java.",  # the old version's rows are not used
            "Data Engineer\nInvented posting with no requirements yet.",
            "Site Engineer\nInvented posting. You need Linux.",  # requirements count once the job says they are done
        ]
    )


def test_a_vector_is_not_written_over_a_job_that_changed_while_it_was_being_embedded(conn, model):
    source = add_source(conn)
    job = add_job(conn, source, 1)

    def a_fetch_changes_the_posting():
        conn.execute(
            text(
                """UPDATE jobs SET description = 'Invented posting, rewritten by a fetch.', content_version = 2,
                                   embedding = NULL, embedding_model = NULL WHERE id = :id"""
            ),
            {"id": job},
        )

    model.during = a_fetch_changes_the_posting
    assert embedding.embed_pending(conn) == 0
    assert tuple(row(conn, job)) == (None, None)  # the vector belonged to the old text, so it was dropped

    model.during = None
    assert embedding.embed_pending(conn) == 1  # the next sweep picks the new version up
    assert row(conn, job).embedding_model == embed.LOCAL_MODEL_ID
    assert model.batches[-1] == ["Invented title 1\nInvented posting, rewritten by a fetch."]


def test_a_vector_that_another_sweep_wrote_first_is_kept(conn, model):
    source = add_source(conn)
    job = add_job(conn, source, 1)

    def another_worker_finishes_first():
        literal = embedding.vector_literal(vector(0.5))
        conn.execute(
            text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
            {"v": literal, "m": embed.LOCAL_MODEL_ID, "id": job},
        )

    model.during = another_worker_finishes_first
    assert embedding.embed_pending(conn) == 0

    assert row(conn, job).embedding_model == embed.LOCAL_MODEL_ID
    assert stored_first_value(conn, job) == 0.5


def test_a_sweep_after_everything_is_embedded_writes_nothing_and_does_not_call_the_model(conn, model):
    source = add_source(conn)
    add_job(conn, source, 1)
    add_job(conn, source, 2)
    assert embedding.embed_pending(conn) == 2
    calls = len(model.batches)

    versions_before = conn.execute(text("SELECT id, xmin::text FROM jobs ORDER BY id")).all()
    assert embedding.embed_pending(conn) == 0

    assert len(model.batches) == calls
    assert conn.execute(text("SELECT id, xmin::text FROM jobs ORDER BY id")).all() == versions_before


def test_one_sweep_takes_a_bounded_batch_oldest_first(conn, model, monkeypatch):
    monkeypatch.setattr(embedding, "SWEEP_BATCH", 2)
    source = add_source(conn)
    ids = [add_job(conn, source, n) for n in (1, 2, 3)]

    assert embedding.embed_pending(conn) == 2
    assert [row(conn, i).embedding_model for i in ids] == [embed.LOCAL_MODEL_ID, embed.LOCAL_MODEL_ID, None]
    assert embedding.embed_pending(conn) == 1
    assert row(conn, ids[2]).embedding_model == embed.LOCAL_MODEL_ID


def test_a_model_that_is_not_there_fails_the_sweep_so_the_queue_records_it(conn, monkeypatch):
    def unavailable(texts, **options):
        raise embed.EmbeddingUnavailable("the model")

    monkeypatch.setattr(embedding, "embed_passages", unavailable)
    add_job(conn, add_source(conn), 1)

    with pytest.raises(embed.EmbeddingUnavailable):
        embedding.embed_sweep(conn, {})


def queued(conn):
    return conn.execute(text("SELECT unique_key FROM queue_jobs WHERE kind = 'ingest.embed_sweep'")).scalars().all()


def test_the_sweep_is_scheduled_every_five_minutes_and_a_repeat_in_the_same_slot_adds_nothing(conn):
    assert scheduler._every["ingest.embed_sweep"] == FIVE_MINUTES
    assert queue._handlers["ingest.embed_sweep"] is embedding.embed_sweep
    assert jobs.EMBED_EVERY_MINUTES == 5

    scheduler.tick(conn, NOW)
    scheduler.tick(conn, NOW)  # another worker, the same moment
    scheduler.tick(conn, NOW + timedelta(minutes=2))  # later in the same slot
    assert len(queued(conn)) == 1

    scheduler.tick(conn, NOW + timedelta(minutes=5))  # the next slot
    assert len(queued(conn)) == 2


@pytest.fixture
def committed_index(engine):
    """The worker's own login commits, so this test cleans up the way the API tests do: empty before and after."""
    empty = text("TRUNCATE queue_jobs, jobs, job_sources RESTART IDENTITY CASCADE")
    with engine.begin() as c:
        c.execute(empty)
    yield
    with engine.begin() as c:
        c.execute(empty)


def test_the_worker_role_holds_every_privilege_the_sweep_needs(engine, committed_index, model):
    with engine.begin() as c:
        source = add_source(c)
        job = add_job(c, source, 1, description="Invented posting. You need Python.")
        add_requirement(c, job, 1, "required", "You need Python")
        c.execute(text("UPDATE jobs SET requirements_version = 1 WHERE id = :id"), {"id": job})

    with db.transaction(role="worker") as conn:
        embedding.embed_sweep(conn, {})

    with engine.connect() as c:
        assert row(c, job).embedding_model == embed.LOCAL_MODEL_ID
    assert model.texts == ["Invented title 1\nYou need Python"]


# ---- a change of model re-embeds every row (AI_SYSTEM 3.1) -----------------------------------------------------------


def literal_of(first, dimensions):
    return embedding.vector_literal(vector(first, dimensions))


def test_the_sweep_names_the_model_it_read_so_a_vector_is_never_stored_under_another_name(conn, model):
    add_job(conn, add_source(conn), 1)

    embedding.embed_pending(conn)

    assert model.options == [{"model": embed.LOCAL_MODEL_ID}]


def test_rows_made_by_another_model_are_embedded_again_and_rows_of_the_active_model_are_left_alone(conn, openai_model):
    source = add_source(conn)
    local_made = add_job(conn, source, 1, embedding=literal_of(0.5, 384), embedding_model=embed.LOCAL_MODEL_ID)
    older = add_job(conn, source, 2, embedding=literal_of(0.5, 384), embedding_model="some-older-model")
    blank = add_job(conn, source, 3)
    current = add_job(conn, source, 4, embedding=literal_of(0.25, 1536), embedding_model=embed.OPENAI_MODEL_ID)
    before = conn.execute(text("SELECT xmin::text FROM jobs WHERE id = :id"), {"id": current}).scalar_one()

    assert embedding.embed_pending(conn) == 3

    for job in (local_made, older, blank):
        found = row(conn, job)
        assert found.embedding_model == embed.OPENAI_MODEL_ID
        assert len(found.embedding.strip("[]").split(",")) == 1536  # the old 384-wide vector is gone, not padded
    assert conn.execute(text("SELECT xmin::text FROM jobs WHERE id = :id"), {"id": current}).scalar_one() == before
    assert stored_first_value(conn, current) == 0.25
    assert openai_model.options == [{"model": embed.OPENAI_MODEL_ID}]
    assert len(openai_model.texts) == 3  # not one for the row that was already current


def test_the_new_model_embeds_only_open_canonical_listed_jobs_like_the_old_one(conn, openai_model):
    source = add_source(conn)
    old = {"embedding": literal_of(0.5, 384), "embedding_model": "old"}
    wanted = add_job(conn, source, 1, **old)
    closed = add_job(conn, source, 2, status="closed", closed_at=NOW, **old)
    duplicate = add_job(conn, source, 3, canonical_job_id=wanted, **old)
    unlisted = add_job(conn, source, 4, is_listed=False, **old)

    assert embedding.embed_pending(conn) == 1

    assert row(conn, wanted).embedding_model == embed.OPENAI_MODEL_ID
    for left_alone in (closed, duplicate, unlisted):
        assert row(conn, left_alone).embedding_model == "old"  # ranking never reads them, so no money is spent on them


def test_a_job_that_changed_while_the_new_model_was_working_keeps_its_newer_version_and_no_stale_vector(
    conn, openai_model
):
    source = add_source(conn)
    job = add_job(conn, source, 1, embedding=literal_of(0.5, 384), embedding_model=embed.LOCAL_MODEL_ID)

    def a_fetch_changes_the_posting():
        conn.execute(
            text(
                """UPDATE jobs SET description = 'Invented posting, rewritten by a fetch.', content_version = 2,
                                   embedding = NULL, embedding_model = NULL WHERE id = :id"""
            ),
            {"id": job},
        )

    openai_model.during = a_fetch_changes_the_posting
    assert embedding.embed_pending(conn) == 0
    assert tuple(row(conn, job)) == (None, None)  # the vector belonged to the old text, so it was dropped

    openai_model.during = None
    assert embedding.embed_pending(conn) == 1
    assert row(conn, job).embedding_model == embed.OPENAI_MODEL_ID
    assert openai_model.batches[-1] == ["Invented title 1\nInvented posting, rewritten by a fetch."]


def test_a_second_sweep_of_the_new_model_does_not_write_over_the_first(conn, openai_model):
    source = add_source(conn)
    job = add_job(conn, source, 1, embedding=literal_of(0.5, 384), embedding_model=embed.LOCAL_MODEL_ID)

    def another_worker_finishes_first():
        conn.execute(
            text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :m WHERE id = :id"),
            {"v": literal_of(0.75, 1536), "m": embed.OPENAI_MODEL_ID, "id": job},
        )

    openai_model.during = another_worker_finishes_first
    assert embedding.embed_pending(conn) == 0
    assert stored_first_value(conn, job) == 0.75


def test_when_the_new_model_is_unavailable_the_old_vectors_stay_so_search_keeps_what_it_has(conn, monkeypatch):
    settings = config.load({"APP_ENV": "test", "EMBEDDING_PROVIDER": "openai"})  # asked for, with no key to serve it
    monkeypatch.setattr(config, "get", lambda: settings)
    job = add_job(conn, add_source(conn), 1, embedding=literal_of(0.5, 384), embedding_model=embed.LOCAL_MODEL_ID)

    with pytest.raises(embed.EmbeddingUnavailable):
        embedding.embed_pending(conn)

    assert row(conn, job).embedding_model == embed.LOCAL_MODEL_ID  # nothing was cleared or written
