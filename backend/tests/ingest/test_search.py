"""IDX-04, IDX-05: search the index by meaning and by keywords at once, and read one job.

Two kinds of test. The retrieval check runs the real local model over an invented corpus (semantic_corpus.json): a
plain-English query that shares no word with its posting must find it in hybrid mode and must not in keyword mode.
Everything else gives jobs hand-made vectors and replaces only the query embedder, so the fusion, the filters, the
cursor and the fallbacks are tested exactly, with no model in the way.

Everything runs in the `conn` fixture's transaction, which is rolled back.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import text

from app.ingest import embedding, search
from app.platform import log
from app.platform.llm.embed import EmbeddingUnavailable

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "semantic_corpus.json").read_text(encoding="utf-8"))
NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
QUERY_VECTOR = [1.0] + [0.0] * 383
JOB_KEYS = {
    "id", "title", "company_name", "location_raw", "country", "city", "work_arrangements", "is_remote",
    "employment_type", "pay", "apply_url", "source", "status", "posted_at", "last_verified_at", "content_version",
}  # fmt: skip


def vec(tilt=0.0):
    """A vector that points at QUERY_VECTOR when tilt is 0 and turns away from it as tilt grows."""
    return [1.0, tilt] + [0.0] * 382


def literal(vector):
    return embedding.vector_literal(vector)


def embedded(vector):
    return {"embedding": literal(vector), "embedding_model": "test-model"}


@pytest.fixture
def query_vector(monkeypatch):
    """Every query embeds to QUERY_VECTOR; the calls are recorded."""
    calls = []

    def embed_query(query):
        calls.append(query)
        return QUERY_VECTOR

    monkeypatch.setattr(search, "embed_query", embed_query)
    return calls


def add_source(conn, ats="lever", token="acme"):
    return conn.execute(
        text(
            "INSERT INTO job_sources (ats, token, company_name) VALUES (CAST(:a AS ats_type), :t, 'Acme') RETURNING id"
        ),
        {"a": ats, "t": token},
    ).scalar_one()


def add_job(conn, source, n, **over):
    fields = {
        "source_id": source,
        "external_id": f"job-{n}",
        "title": f"Invented title {n}",
        "company_name": "Acme",
        "description": f"Invented description {n}, long enough to be a posting.",
        "apply_url": f"https://example.test/apply/{n}",
        "content_hash": f"hash-{n}",
        "dedupe_key": f"key-{n}",
        **over,
    }
    arrangements = "{" + ",".join(fields.pop("work_arrangements", [])) + "}"
    names = ", ".join([*fields, "work_arrangements"])
    values = ", ".join([*(f":{name}" for name in fields), "CAST(:work_arrangements AS work_arrangement[])"])
    return conn.execute(
        text(f"INSERT INTO jobs ({names}) VALUES ({values}) RETURNING id"),
        {**fields, "work_arrangements": arrangements},
    ).scalar_one()


def add_requirement(conn, job_id, ordinal, importance, quote, *, kind="skill", min_years=None, version=1):
    description = conn.execute(text("SELECT description FROM jobs WHERE id = :id"), {"id": job_id}).scalar_one()
    start = description.index(quote)
    conn.execute(
        text(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span, min_years,
                                             prompt_version)
               VALUES (:job, :version, :ordinal, CAST(:importance AS requirement_importance),
                       CAST(:kind AS requirement_kind), :quote, int4range(:start, :end), :years, 'invented-test')"""
        ),
        {"job": job_id, "version": version, "ordinal": ordinal, "importance": importance, "kind": kind,
         "quote": quote, "start": start, "end": start + len(quote), "years": min_years},
    )  # fmt: skip


def run(conn, **over):
    arguments = {"q": "", "country": None, "city": None, "remote": None, "limit": 20, "cursor": None, "mode": "hybrid"}
    return search.search(conn, **{**arguments, **over})


def ids(page):
    return [row["id"] for row in page.rows]


# --- retrieval by meaning, with the real model ----------------------------------------------------------------------


@pytest.fixture
def corpus(conn):
    """The twelve invented postings, embedded by the sweep with the real model: {key: job id}."""
    source = add_source(conn)
    found = {
        posting["key"]: add_job(
            conn,
            source,
            n,
            title=posting["title"],
            department=posting["department"],
            description=posting["description"],
        )
        for n, posting in enumerate(CORPUS["postings"])
    }
    assert embedding.embed_pending(conn) == len(found)
    return found


def shared_lexemes(conn, query, posting):
    """Words (as PostgreSQL's English text search stems them) that the query and the posting have in common."""
    blob = f"{posting['title']} {posting['department']} {posting['description']}"
    return conn.execute(
        text(
            """SELECT array(SELECT unnest(tsvector_to_array(to_tsvector('english', :q)))
                            INTERSECT
                            SELECT unnest(tsvector_to_array(to_tsvector('english', :t))))"""
        ),
        {"q": query, "t": blob},
    ).scalar_one()


@pytest.mark.parametrize("item", CORPUS["queries"], ids=lambda item: item["target"])
def test_a_plain_english_query_finds_its_posting_by_meaning_and_keyword_search_does_not(conn, corpus, item):
    posting = next(p for p in CORPUS["postings"] if p["key"] == item["target"])
    target = corpus[item["target"]]
    assert shared_lexemes(conn, item["query"], posting) == [], "the query must share no word with its posting"

    keyword = run(conn, q=item["query"], mode="keyword", limit=100)
    hybrid = run(conn, q=item["query"], mode="hybrid", limit=100)

    assert keyword.mode == "keyword" and target not in ids(keyword)
    assert hybrid.mode == "hybrid" and target in ids(hybrid)[:3]


# --- fusion ---------------------------------------------------------------------------------------------------------


def test_a_keyword_only_job_and_a_vector_only_job_both_appear_and_a_job_in_both_lists_comes_first(conn, query_vector):
    source = add_source(conn)
    keyword_only = add_job(conn, source, 1, title="Alpha")  # no vector yet, so it is in the keyword list only
    vector_only = add_job(conn, source, 2, title="Unrelated", **embedded(vec(0.0)))  # nothing for the keyword list
    both = add_job(conn, source, 3, title="Alpha engineer", **embedded(vec(0.1)))
    far = add_job(conn, source, 4, title="Zeta", **embedded(vec(5.0)))

    page = run(conn, q="alpha")

    assert page.mode == "hybrid"
    assert ids(page)[0] == both  # in both lists: 1/(60+2) twice, against 1/(60+1) once
    assert set(ids(page)[1:3]) == {keyword_only, vector_only}  # each one list, rank 1: the same score
    assert ids(page)[3] == far
    assert len(page.rows) == 4


def test_each_list_keeps_its_best_two_hundred_jobs(conn, query_vector):
    assert (search.RRF_K, search.LIST_DEPTH) == (60, 200)
    source = add_source(conn)
    for n in range(
        search.LIST_DEPTH + 5
    ):  # the first 200 are nearest in vector and first in id: the best of both lists
        add_job(conn, source, n, title="Alpha", **embedded(vec(0.001 * n)))

    found = pages(conn, 100, q="alpha", mode="hybrid")

    assert len(found) == search.LIST_DEPTH and len(set(found)) == search.LIST_DEPTH  # the last five are in neither list


def test_the_keyword_list_keeps_the_two_hundred_best_matches_not_the_first_two_hundred(conn):
    source = add_source(conn)
    for n in range(search.LIST_DEPTH):
        add_job(conn, source, n, title="Beta", description="Invented posting that mentions alpha once.")
    strongest = [add_job(conn, source, 1000 + n, title="Alpha") for n in range(5)]  # the last ids, the title matches

    found = pages(conn, 100, q="alpha", mode="keyword")

    assert found[:5] == strongest and len(found) == search.LIST_DEPTH


def test_the_constant_is_sixty_so_two_middling_ranks_beat_one_top_rank_but_two_poor_ones_do_not(conn, query_vector):
    """One list's rank 1 scores 1/61. A job at rank 60 in both lists scores 2/120, which is more; at rank 63 in both it
    scores 2/123, which is less. Another k would reverse one of the two."""
    source = add_source(conn)
    top = add_job(conn, source, 0, title="Unrelated", **embedded(vec(0.001)))  # dense rank 1, not in the keyword list
    # In insertion order, with the dense rank each one gets (its sparse rank is its place among these, 1 to 63).
    dense_ranks = [*range(2, 60), 61, 60, 62, 64, 63]  # 58 helpers, one more helper, `middling`, two fillers, `poor`
    jobs = [
        add_job(conn, source, n, title="Alpha", **embedded(vec(0.001 * rank))) for n, rank in enumerate(dense_ranks, 1)
    ]
    middling, poor = jobs[59], jobs[62]

    order = ids(run(conn, q="alpha", limit=100))

    assert order.index(middling) < order.index(top) < order.index(poor)


def test_keyword_mode_puts_a_title_match_above_a_description_match(conn):
    source = add_source(conn)
    in_description = add_job(conn, source, 1, title="Beta", description="Invented posting that mentions alpha once.")
    in_title = add_job(conn, source, 2, title="Alpha")  # a later id, but the title weighs more than the description

    assert ids(run(conn, q="alpha", mode="keyword")) == [in_title, in_description]


# --- filters --------------------------------------------------------------------------------------------------------


@pytest.fixture
def places(conn, query_vector):
    """Four jobs that all match "alpha", from the nearest vector to the farthest."""
    source = add_source(conn)

    def job(n, name, tilt, **place):
        return add_job(conn, source, n, title=f"Alpha {name}", **place, **embedded(vec(tilt)))

    return {
        "us_remote": job(1, "one", 0.0, country="US", city="Austin", work_arrangements=["remote"]),
        "in_onsite": job(2, "two", 0.2, country="IN", city="Pune", work_arrangements=["onsite", "hybrid"]),
        "in_remote": job(3, "three", 0.4, country="IN", city="Bengaluru", work_arrangements=["remote"]),
        "gb_unstated": job(4, "four", 0.6, country="GB", city="London"),
    }


@pytest.mark.parametrize("mode", ["hybrid", "keyword"])
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, ["us_remote", "in_onsite", "in_remote", "gb_unstated"]),
        ({"country": "in"}, ["in_onsite", "in_remote"]),
        ({"city": "PUNE"}, ["in_onsite"]),
        ({"remote": True}, ["us_remote", "in_remote"]),
        ({"remote": False}, ["in_onsite"]),  # the London posting does not say, so it is neither
        ({"country": "IN", "remote": True}, ["in_remote"]),
        ({"country": "FR"}, []),
    ],
)
def test_country_city_and_remote_filters_apply_in_both_modes(conn, places, mode, filters, expected):
    page = run(conn, q="alpha", mode=mode, **filters)

    assert ids(page) == [places[name] for name in expected]  # all four match "alpha", so the order is the ranking


def test_the_filters_apply_before_the_ranking_not_after_it(conn, query_vector):
    """Two hundred nearer jobs in another country must not push the one Indian job out of the dense list."""
    source = add_source(conn)
    for n in range(search.LIST_DEPTH + 10):
        add_job(conn, source, n, title="Gamma", country="US", **embedded(vec(0.001 * n)))
    indian = add_job(conn, source, 999, title="Delta", country="IN", **embedded(vec(9.0)))
    # Give the planner every reason to use the approximate vector index. An index scan returns its few nearest rows
    # first and filters afterwards, so it would find none of the Indian jobs; the exact scan over the filtered rows
    # (the materialized eligible set) is what makes this pass.
    conn.execute(text("SET LOCAL enable_seqscan = off"))

    assert ids(run(conn, q="unmatched words", country="IN")) == [indian]  # found by meaning alone, from the far end


# --- fallbacks ------------------------------------------------------------------------------------------------------


def test_with_no_embedded_job_a_hybrid_request_answers_in_keyword_mode_without_loading_the_model(conn, query_vector):
    source = add_source(conn)
    found = add_job(conn, source, 1, title="Alpha")
    add_job(conn, source, 2, title="Beta")

    page = run(conn, q="alpha", mode="hybrid")

    assert (page.mode, ids(page)) == ("keyword", [found])
    assert query_vector == []  # the model was not asked for anything


def test_embedded_jobs_that_cannot_be_searched_do_not_count_as_embeddings(conn, query_vector):
    source = add_source(conn)
    add_job(conn, source, 1, title="Alpha", status="closed", closed_at=NOW, **embedded(vec()))
    add_job(conn, source, 2, title="Alpha", is_listed=False, **embedded(vec()))

    assert run(conn, q="alpha", mode="hybrid").mode == "keyword"


def test_when_the_model_is_unavailable_a_hybrid_request_falls_back_to_keywords(conn, monkeypatch):
    def unavailable(query):
        raise EmbeddingUnavailable("the model")

    monkeypatch.setattr(search, "embed_query", unavailable)
    source = add_source(conn)
    found = add_job(conn, source, 1, title="Alpha", **embedded(vec()))
    add_job(conn, source, 2, title="Beta", **embedded(vec()))

    page = run(conn, q="alpha", mode="hybrid")

    assert (page.mode, ids(page)) == ("keyword", [found])


def test_an_empty_query_lists_newest_first_then_by_id_and_reports_the_mode_that_was_asked_for(conn, query_vector):
    source = add_source(conn)
    old = add_job(conn, source, 1, posted_at=NOW - timedelta(days=9), **embedded(vec()))
    tie_low = add_job(conn, source, 2, posted_at=NOW - timedelta(days=1))
    tie_high = add_job(conn, source, 3, posted_at=NOW - timedelta(days=1))
    undated = add_job(conn, source, 4)
    newest = add_job(conn, source, 5, posted_at=NOW)

    for mode in ("hybrid", "keyword"):
        page = run(conn, q="   ", mode=mode)
        assert ids(page) == [newest, tie_high, tie_low, old, undated]  # no date comes last
        assert page.mode == mode
    assert query_vector == []


def test_a_mode_that_is_neither_hybrid_nor_keyword_is_a_programming_error(conn):
    with pytest.raises(ValueError):
        run(conn, q="alpha", mode="semantic")


# --- cursor ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def six(conn, query_vector):
    """Six jobs that all match "alpha", all in Austin and remote. The keyword list ranks them 1 to 6 by id; the vector
    list ranks them 6 to 1, so the fused scores tie in pairs and the order is [1, 6, 2, 5, 3, 4]: a page boundary can
    fall inside a tie."""
    source = add_source(conn)
    return [
        add_job(
            conn, source, n, title="Alpha", country="US", city="Austin", work_arrangements=["remote"],
            posted_at=NOW - timedelta(days=n % 3), **embedded(vec(0.1 * (7 - n))),
        )
        for n in range(1, 7)
    ]  # fmt: skip


def pages(conn, limit, **request):
    found, cursor = [], None
    while True:
        page = run(conn, limit=limit, cursor=cursor, **request)
        assert len(page.rows) <= limit
        found.extend(ids(page))
        cursor = page.next_cursor
        if cursor is None:
            return found


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.parametrize(
    "request_", [{"q": "alpha", "mode": "hybrid"}, {"q": "alpha", "mode": "keyword"}, {"q": "", "mode": "hybrid"}]
)
def test_pages_of_any_size_return_the_same_jobs_in_the_same_order_as_one_page(conn, six, request_, limit):
    whole = run(conn, limit=6, **request_)
    assert len(whole.rows) == 6 and whole.next_cursor is None

    assert pages(conn, limit, **request_) == ids(whole)
    assert len(set(ids(whole))) == 6


def test_the_fused_order_puts_the_pairs_with_the_same_score_together_lowest_id_first(conn, six):
    assert ids(run(conn, q="alpha", mode="hybrid")) == [six[0], six[5], six[1], six[4], six[2], six[3]]


@pytest.mark.parametrize(
    "change",
    [{"q": "beta"}, {"country": "US"}, {"city": "Austin"}, {"remote": True}, {"mode": "keyword"}],
)
def test_a_cursor_from_another_search_is_refused(conn, six, change):
    cursor = run(conn, q="alpha", limit=2).next_cursor
    assert cursor is not None

    with pytest.raises(search.InvalidCursor):
        run(conn, **{"q": "alpha", "limit": 2, "cursor": cursor, **change})


def test_a_cursor_does_not_care_how_the_query_is_spaced(conn, six):
    cursor = run(conn, q="alpha", limit=2).next_cursor

    assert ids(run(conn, q="  alpha ", limit=2, cursor=cursor)) == ids(run(conn, q="alpha", limit=2, cursor=cursor))


@pytest.mark.parametrize(
    "cursor",
    [
        "not-a-cursor",
        "!!!",
        "e30",  # {}
        "W10",  # []
        "eyJtIjoiaHlicmlkIn0",  # {"m":"hybrid"}: nothing else
        "bnVsbA",  # null
    ],
)
def test_a_cursor_that_does_not_decode_is_refused(conn, six, cursor):
    with pytest.raises(search.InvalidCursor):
        run(conn, q="alpha", cursor=cursor)


def test_a_cursor_with_a_position_of_the_wrong_type_is_refused(conn, six):
    import base64

    real = run(conn, q="alpha", limit=2).next_cursor
    body = json.loads(base64.urlsafe_b64decode(real + "=" * (-len(real) % 4)))
    for key, bad in (("k", "text"), ("k", None), ("k", float("nan")), ("i", "7"), ("i", True), ("i", 1.5)):
        forged = base64.urlsafe_b64encode(json.dumps({**body, key: bad}).encode()).decode().rstrip("=")
        with pytest.raises(search.InvalidCursor):
            run(conn, q="alpha", cursor=forged)


def test_a_listing_cursor_with_a_date_that_is_not_a_date_is_refused(conn, six):
    import base64

    real = run(conn, q="", limit=2).next_cursor
    body = json.loads(base64.urlsafe_b64decode(real + "=" * (-len(real) % 4)))
    forged = base64.urlsafe_b64encode(json.dumps({**body, "k": "yesterday"}).encode()).decode().rstrip("=")

    with pytest.raises(search.InvalidCursor):
        run(conn, q="", cursor=forged)


# --- who may appear -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("request_", [{"q": "alpha", "mode": "hybrid"}, {"q": "alpha", "mode": "keyword"}, {"q": ""}])
def test_closed_duplicate_and_unlisted_jobs_never_appear(conn, query_vector, request_):
    source = add_source(conn)
    shown = add_job(conn, source, 1, title="Alpha", **embedded(vec()))
    add_job(conn, source, 2, title="Alpha", status="closed", closed_at=NOW, **embedded(vec()))
    add_job(conn, source, 3, title="Alpha", canonical_job_id=shown, **embedded(vec()))
    add_job(conn, source, 4, title="Alpha", is_listed=False, **embedded(vec()))

    assert ids(run(conn, **request_)) == [shown]


# --- what a job looks like ------------------------------------------------------------------------------------------


def test_a_job_carries_its_source_its_verified_time_and_the_typed_fields(conn):
    source = add_source(conn, "greenhouse", "northwind")
    job = add_job(
        conn, source, 1, title="Alpha", country="IN", city="Pune", location_raw="Pune, India (Remote)",
        work_arrangements=["remote"], employment_type="full_time", salary_min=100000, salary_max=150000,
        salary_currency="USD", posted_at=NOW,
    )  # fmt: skip
    undated = add_job(conn, source, 2, title="Alpha two")
    conn.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))  # the answer is UTC whatever the session says

    rows = {row["id"]: row for row in run(conn, q="alpha").rows}

    assert set(rows[job]) == JOB_KEYS
    assert rows[job]["source"] == {"ats": "greenhouse", "board": "northwind"}
    assert rows[job]["work_arrangements"] == ["remote"] and rows[job]["is_remote"] is True
    assert rows[job]["employment_type"] == "full_time" and rows[job]["status"] == "open"
    assert rows[job]["pay"] == {"min": 100000, "max": 150000, "currency": "USD"}
    assert rows[job]["posted_at"] == NOW and rows[job]["last_verified_at"].utcoffset() == timedelta(0)
    assert (rows[job]["country"], rows[job]["city"], rows[job]["content_version"]) == ("IN", "Pune", 1)
    assert rows[undated]["pay"] is None and rows[undated]["is_remote"] is None and rows[undated]["posted_at"] is None
    assert rows[undated]["work_arrangements"] == [] and rows[undated]["employment_type"] == "unknown"


# --- one job --------------------------------------------------------------------------------------------------------


def test_the_detail_has_the_description_and_the_requirements_of_the_current_version_with_their_spans(conn):
    source = add_source(conn)
    description = "Invented 🙂 café posting. You need Python. We would like Rust. Five years of SQL."
    job = add_job(conn, source, 1, description=description)
    add_requirement(conn, job, 1, "required", "You need Python")
    add_requirement(conn, job, 2, "preferred", "We would like Rust")
    add_requirement(conn, job, 3, "required", "Five years of SQL", kind="experience", min_years=5)
    conn.execute(text("UPDATE jobs SET requirements_version = 1 WHERE id = :id"), {"id": job})

    detail = search.get_job(conn, job)

    assert detail is not None and set(detail) == JOB_KEYS | {
        "description",
        "closed_at",
        "requirements_version",
        "requirements",
    }
    assert detail["description"] == description and detail["closed_at"] is None and detail["requirements_version"] == 1
    assert [(r["ordinal"], r["importance"], r["kind"], r["min_years"]) for r in detail["requirements"]] == [
        (1, "required", "skill", None),
        (2, "preferred", "skill", None),
        (3, "required", "experience", 5),
    ]
    for requirement in detail["requirements"]:  # the span is the quote, counted in characters
        assert set(requirement) == {"id", "ordinal", "text", "importance", "kind", "span", "min_years"}
        assert description[requirement["span"]["start"] : requirement["span"]["end"]] == requirement["text"]


def test_requirements_of_an_older_version_or_of_an_unfinished_extraction_are_not_shown(conn):
    source = add_source(conn)
    job = add_job(conn, source, 1, description="Invented posting. You need Python.")
    add_requirement(conn, job, 1, "required", "You need Python")
    assert search.get_job(conn, job)["requirements"] == []  # rows exist, but the job does not say they are complete

    conn.execute(text("UPDATE jobs SET requirements_version = 1 WHERE id = :id"), {"id": job})
    assert len(search.get_job(conn, job)["requirements"]) == 1

    conn.execute(  # the posting is rewritten: version 2 has no requirements yet
        text("UPDATE jobs SET description = 'Invented posting, rewritten.', content_version = 2 WHERE id = :id"),
        {"id": job},
    )
    detail = search.get_job(conn, job)
    assert detail["requirements"] == [] and detail["requirements_version"] == 1 and detail["content_version"] == 2


def test_a_closed_job_is_still_readable_and_a_missing_or_unlisted_one_is_not(conn):
    source = add_source(conn)
    closed = add_job(conn, source, 1, status="closed", closed_at=NOW)
    unlisted = add_job(conn, source, 2, is_listed=False)

    detail = search.get_job(conn, closed)

    assert detail is not None and detail["status"] == "closed" and detail["closed_at"] == NOW
    assert search.get_job(conn, unlisted) is None
    assert search.get_job(conn, 987654321) is None


# --- the log --------------------------------------------------------------------------------------------------------


def test_a_search_leaves_no_trace_of_the_query_in_the_log(conn, query_vector, caplog):
    canary = "canaryqueryz9 plumber"
    source = add_source(conn)
    add_job(conn, source, 1, title="Plumber", country="IN", city="Pune", **embedded(vec()))
    captured = StringIO()
    caplog.set_level(logging.DEBUG)
    previous = log._handler.setStream(captured)
    try:
        run(conn, q=canary, country="IN", city="Pune", mode="hybrid")
        run(conn, q=canary, mode="keyword")
    finally:
        log._handler.setStream(previous)

    everything = (captured.getvalue() + caplog.text).lower()
    assert "canaryqueryz9" not in everything and "plumber" not in everything and "pune" not in everything
    lines = [json.loads(line) for line in captured.getvalue().splitlines()]
    assert [(line["event"], line["method"]) for line in lines] == [
        ("jobs.search", "hybrid"),
        ("jobs.search", "keyword"),
    ]
    assert all(set(line) >= {"result_count", "duration_ms"} for line in lines)
