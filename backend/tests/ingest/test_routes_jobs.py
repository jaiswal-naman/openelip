"""GET /v1/jobs and GET /v1/jobs/{job_id} (API.md 2.4), through the real API on the test database.

search() and get_job() are tested on their own in test_search.py. These tests are about what the API puts around them:
the envelope, the fields every job carries (IDX-05), paging by cursor, the errors, and the schema the web types are
generated from. The old keys of /v1/jobs and the seven keys of /v1/stats must all still be there.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.ingest import embedding, search
from app.main import create_app
from app.platform import config
from app.platform.llm.embed import LOCAL_MODEL_ID, OPENAI_MODEL_ID
from tests.ingest.test_search_constrained import sign_in
from tests.match.factories import make_user

NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
OLD_JOB_KEYS = {
    "id", "title", "company_name", "location_raw", "country", "city", "is_remote", "apply_url", "posted_at",
    "last_verified_at", "content_version",
}  # fmt: skip
JOB_KEYS = OLD_JOB_KEYS | {"work_arrangements", "employment_type", "pay", "source", "status"}
DETAIL_KEYS = JOB_KEYS | {"description", "closed_at", "requirements_version", "requirements"}
STATS_KEYS = {
    "open_postings", "open_unique_jobs", "open_unique_jobs_india", "open_unique_jobs_remote", "closed_postings",
    "employers_with_open_jobs", "last_verified_at",
}  # fmt: skip
QUERY_VECTOR = [1.0] + [0.0] * 383


def vec(tilt):
    return [1.0, tilt] + [0.0] * 382


@pytest.fixture
def query_vector(monkeypatch):
    monkeypatch.setattr(search, "embed_query", lambda query, **options: QUERY_VECTOR)


def add_source(c, ats="lever", token="acme"):
    return c.execute(
        text(
            "INSERT INTO job_sources (ats, token, company_name) VALUES (CAST(:a AS ats_type), :t, 'Acme') RETURNING id"
        ),
        {"a": ats, "t": token},
    ).scalar_one()


def add_job(c, source, n, **over):
    fields = {
        "source_id": source,
        "external_id": f"job-{n}",
        "title": f"Alpha engineer {n}",
        "company_name": "Acme",
        "description": f"Invented description {n}, long enough to be a posting.",
        "apply_url": f"https://example.test/apply/{n}",
        "content_hash": f"hash-{n}",
        "dedupe_key": f"key-{n}",
        **over,
    }
    names = ", ".join(fields)
    values = ", ".join(f":{name}" for name in fields)
    return c.execute(text(f"INSERT INTO jobs ({names}) VALUES ({values}) RETURNING id"), fields).scalar_one()


def embed_by_hand(c, job_id, vector):
    c.execute(
        text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :model WHERE id = :id"),
        {"v": embedding.vector_literal(vector), "model": LOCAL_MODEL_ID, "id": job_id},
    )


@pytest.fixture
def five(engine, api_client):
    """Five open jobs on one board that all match "alpha", the first one the newest."""
    with engine.begin() as c:
        source = add_source(c, "greenhouse", "northwind")
        return [add_job(c, source, n, posted_at=NOW - timedelta(days=n)) for n in range(1, 6)]


def get_ok(api_client, url, **params):
    response = api_client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_listed_job_keeps_the_old_keys_and_carries_its_source_and_when_it_was_last_verified_open(api_client, five):
    body = get_ok(api_client, "/v1/jobs")

    assert body["success"] is True and body["error"] is None
    assert set(body["meta"]) == {"request_id", "count", "limit", "mode", "next_cursor"}
    assert (body["meta"]["count"], body["meta"]["limit"], body["meta"]["next_cursor"]) == (5, 20, None)
    for job in body["data"]:
        assert set(job) == JOB_KEYS and OLD_JOB_KEYS <= set(job)
        assert job["source"] == {"ats": "greenhouse", "board": "northwind"}
        assert job["status"] == "open" and job["work_arrangements"] == [] and job["is_remote"] is None
        assert job["pay"] is None and job["employment_type"] == "unknown"
        assert job["last_verified_at"].endswith("Z")  # RFC 3339 in UTC (API.md 1.4)
        datetime.fromisoformat(job["last_verified_at"])


def test_meta_mode_is_keyword_until_jobs_have_vectors_and_hybrid_after(api_client, engine, five, query_vector):
    assert get_ok(api_client, "/v1/jobs", q="alpha")["meta"]["mode"] == "keyword"  # nothing embedded yet

    with engine.begin() as c:
        for tilt, job_id in enumerate(five):
            embed_by_hand(c, job_id, vec(0.1 * tilt))

    assert get_ok(api_client, "/v1/jobs", q="alpha")["meta"]["mode"] == "hybrid"
    assert get_ok(api_client, "/v1/jobs", q="alpha", mode="keyword")["meta"]["mode"] == "keyword"
    assert get_ok(api_client, "/v1/jobs", q="alpha", mode="hybrid")["meta"]["mode"] == "hybrid"
    assert get_ok(api_client, "/v1/jobs")["meta"]["mode"] == "hybrid"  # no query: the mode asked for, which is hybrid


def test_the_filters_and_the_limit_reach_the_search(api_client, engine, five):
    with engine.begin() as c:
        c.execute(
            text(
                """UPDATE jobs SET country = 'IN', city = CASE id WHEN :pune THEN 'Pune' ELSE 'Mumbai' END,
                          work_arrangements = CASE id WHEN :pune THEN '{remote}' ELSE '{onsite}' END::work_arrangement[]
                    WHERE id IN (:pune, :mumbai)"""
            ),
            {"pune": five[0], "mumbai": five[1]},
        )

    def found(**params):
        return {job["id"] for job in get_ok(api_client, "/v1/jobs", **params)["data"]}

    assert found(country="in") == {five[0], five[1]}
    assert found(country="in", city="pune") == {five[0]}  # the city narrows within the country
    assert found(country="IN", remote="true") == {five[0]}
    assert found(country="IN", remote="false") == {five[1]}
    body = get_ok(api_client, "/v1/jobs", limit=2)
    assert (body["meta"]["count"], body["meta"]["limit"]) == (2, 2) and body["meta"]["next_cursor"] is not None


@pytest.mark.parametrize(
    "params",
    [
        {"q": "x" * 201},
        {"city": "x" * 101},
        {"country": "I"},
        {"country": "IND"},
        {"country": "1A"},
        {"country": "\x00\x00"},
        {"limit": 0},
        {"limit": 101},
    ],
)
def test_a_parameter_out_of_range_is_a_422_validation_failed(api_client, five, params):
    response = api_client.get("/v1/jobs", params=params)

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


def test_text_typed_into_the_search_box_cannot_make_the_server_fail(api_client, five):
    """PostgreSQL text cannot hold a NUL character, and anyone can send one."""
    for params in ({"q": "alpha\x00"}, {"q": "\x00"}, {"q": "alpha", "city": "Pune\x00"}, {"cursor": "\x00"}):
        response = api_client.get("/v1/jobs", params=params)
        assert response.status_code in (200, 400), (params, response.status_code)


def test_following_the_cursor_returns_every_job_once_in_the_order_of_one_big_page(api_client, five):
    whole = [job["id"] for job in get_ok(api_client, "/v1/jobs", limit=5)["data"]]
    found, cursor = [], None
    for _ in range(3):
        body = get_ok(api_client, "/v1/jobs", limit=2, **({"cursor": cursor} if cursor else {}))
        found += [job["id"] for job in body["data"]]
        cursor = body["meta"]["next_cursor"]
    assert cursor is None and found == whole and len(set(found)) == 5


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        {"cursor": "e30"},
        {"mode": "semantic"},
        {"mode": ""},
        {"mode": "HYBRID"},
    ],
)
def test_a_cursor_that_does_not_decode_and_an_unknown_mode_are_a_400_invalid_request(api_client, five, params):
    response = api_client.get("/v1/jobs", params=params)

    body = response.json()
    assert response.status_code == 400
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, "invalid_request")


def test_a_cursor_from_another_search_is_a_400_invalid_request(api_client, five):
    cursor = get_ok(api_client, "/v1/jobs", limit=2, q="alpha")["meta"]["next_cursor"]

    response = api_client.get("/v1/jobs", params={"limit": 2, "q": "beta", "cursor": cursor})

    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


@pytest.fixture
def described(engine, api_client):
    """One job with a description and requirements, and a closed one."""
    description = "Invented posting. You need Python. We would like Rust."
    with engine.begin() as c:
        source = add_source(c)
        job = add_job(c, source, 1, description=description, salary_min=90000, salary_max=120000, salary_currency="EUR")
        for ordinal, importance, quote in ((1, "required", "You need Python"), (2, "preferred", "We would like Rust")):
            start = description.index(quote)
            c.execute(
                text(
                    """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                     prompt_version)
                       VALUES (:job, 1, :ordinal, CAST(:importance AS requirement_importance), 'skill', :quote,
                               int4range(:start, :end), 'invented-test')"""
                ),
                {"job": job, "ordinal": ordinal, "importance": importance, "quote": quote, "start": start,
                 "end": start + len(quote)},
            )  # fmt: skip
        c.execute(text("UPDATE jobs SET requirements_version = 1 WHERE id = :id"), {"id": job})
        closed = add_job(c, source, 2, status="closed", closed_at=NOW, description="Invented closed posting.")
        unlisted = add_job(c, source, 3, is_listed=False)
    return {"job": job, "closed": closed, "unlisted": unlisted, "description": description}


def test_the_detail_has_the_description_the_requirements_and_their_spans(api_client, described):
    body = get_ok(api_client, f"/v1/jobs/{described['job']}")

    job = body["data"]
    assert set(job) == DETAIL_KEYS and body["error"] is None and set(body["meta"]) == {"request_id"}
    assert job["description"] == described["description"] and job["closed_at"] is None
    assert job["source"] == {"ats": "lever", "board": "acme"}
    assert job["pay"] == {"min": 90000, "max": 120000, "currency": "EUR"}
    assert [(r["ordinal"], r["importance"], r["kind"], r["min_years"]) for r in job["requirements"]] == [
        (1, "required", "skill", None),
        (2, "preferred", "skill", None),
    ]
    for requirement in job["requirements"]:
        span = requirement["span"]
        assert job["description"][span["start"] : span["end"]] == requirement["text"]


def test_a_closed_job_is_a_200_with_its_status_and_when_it_closed(api_client, described):
    job = get_ok(api_client, f"/v1/jobs/{described['closed']}")["data"]

    assert job["status"] == "closed" and job["closed_at"] == "2030-01-01T10:30:00Z" and job["requirements"] == []


@pytest.mark.parametrize("which", ["unlisted", "missing"])
def test_a_missing_or_unlisted_job_is_a_404_not_found(api_client, described, which):
    job_id = described["unlisted"] if which == "unlisted" else 987654321

    response = api_client.get(f"/v1/jobs/{job_id}")

    body = response.json()
    assert response.status_code == 404
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, "not_found")


@pytest.mark.parametrize("job_id", ["abc", "0", "99999999999999999999"])
def test_an_id_that_cannot_be_a_job_id_is_a_422_not_a_server_error(api_client, described, job_id):
    response = api_client.get(f"/v1/jobs/{job_id}")

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


def test_stats_keep_their_seven_keys(api_client, five):
    data = get_ok(api_client, "/v1/stats")["data"]

    assert set(data) == STATS_KEYS and data["open_postings"] == 5


def test_the_schema_marks_third_party_text_so_the_web_types_and_reviewers_can_see_it(api_client):
    schemas = create_app().openapi()["components"]["schemas"]

    assert schemas["JobDetail"]["properties"]["description"]["x-untrusted-text"] is True
    assert schemas["Requirement"]["properties"]["text"]["x-untrusted-text"] is True
    assert "x-untrusted-text" not in schemas["Job"]["properties"]["title"]  # only what API.md 1.10 names


def test_the_schema_names_the_modes_and_the_job_shape_exactly(api_client):
    document = create_app().openapi()
    parameters = {p["name"]: p for p in document["paths"]["/v1/jobs"]["get"]["parameters"]}

    # apply_constraints is plan 05-03's (CHT-02); test_search_constrained.py tests it
    assert set(parameters) == {"q", "country", "city", "remote", "limit", "cursor", "mode", "apply_constraints"}
    assert parameters["mode"]["schema"]["enum"] == ["hybrid", "keyword"]
    assert set(document["components"]["schemas"]["Job"]["properties"]) == JOB_KEYS
    assert "/v1/jobs/{job_id}" in document["paths"]


def test_with_the_openai_model_the_query_vector_is_charged_to_the_signed_in_person_and_to_nobody_otherwise(
    api_client, engine, five, monkeypatch
):
    """The route finds the person from the session cookie, whether or not constraints were asked for, and hands it to
    the search; the search hands it to the embedder, whose ledger row is theirs. Nobody signed in: no one to charge."""
    settings = config.load(
        {**os.environ, "EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"}
    )
    monkeypatch.setattr(config, "get", lambda: settings)
    asked = []

    def embed_query(query, **options):
        asked.append(options["principal"])
        return [1.0] + [0.0] * 1535

    monkeypatch.setattr(search, "embed_query", embed_query)
    with engine.begin() as c:
        c.execute(
            text("UPDATE jobs SET embedding = CAST(:v AS vector), embedding_model = :m"),
            {"v": embedding.vector_literal([1.0] + [0.0] * 1535), "m": OPENAI_MODEL_ID},
        )
        person = make_user(c)

    assert api_client.get("/v1/jobs", params={"q": "alpha"}).status_code == 200
    sign_in(api_client, engine, person)
    assert api_client.get("/v1/jobs", params={"q": "alpha"}).status_code == 200
    assert api_client.get("/v1/jobs", params={"q": "alpha", "apply_constraints": "true"}).status_code == 200
    assert api_client.get("/v1/jobs", params={"q": "alpha", "mode": "keyword"}).status_code == 200

    assert asked == [None, person, person]  # keyword mode embeds nothing
