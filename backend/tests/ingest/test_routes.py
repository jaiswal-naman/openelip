"""/v1/jobs and /v1/stats against a real index: open, de-duplicated jobs only, and unknown stays unknown."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.ingest.normalize import work_arrangements_from

JOB_KEYS = {
    "id", "title", "company_name", "location_raw", "country", "city", "is_remote", "apply_url", "posted_at",
    "last_verified_at", "content_version",
}  # fmt: skip


@pytest.fixture
def index(engine, api_client):
    """Four postings: an open Indian remote one, an open American one that does not say, a closed one, a duplicate."""
    with engine.begin() as c:
        source = c.execute(
            text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
        ).scalar_one()

        def add(external_id, title, *, country=None, is_remote=None, status="open", canonical=None):
            closed_at = datetime.now(timezone.utc) if status == "closed" else None
            return c.execute(
                text(
                    "INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, "
                    "content_hash, dedupe_key, country, work_arrangements, status, closed_at, canonical_job_id) "
                    "VALUES (:s, :e, :t, 'Acme', 'Build and run things', 'https://example.test/apply', 'h', 'd', "
                    ":c, CAST(:r AS work_arrangement[]), CAST(:st AS job_status), :ca, :can) RETURNING id"
                ),
                {"s": source, "e": external_id, "t": title, "c": country, "r": work_arrangements_from(is_remote),
                 "st": status, "ca": closed_at, "can": canonical},
            ).scalar_one()  # fmt: skip

        first = add("1", "Backend Engineer", country="IN", is_remote=True)
        second = add("2", "Product Designer", country="US")
        add("3", "Old Engineer", status="closed")
        add("4", "Backend Engineer", country="IN", is_remote=True, canonical=first)
    return {"first": first, "second": second}


def _ids(response):
    assert response.status_code == 200
    return {job["id"] for job in response.json()["data"]}


def test_search_returns_open_unique_jobs_in_the_envelope(api_client, index):
    response = api_client.get("/v1/jobs", params={"q": "engineer"})
    body = response.json()
    assert _ids(response) == {index["first"]}  # not the closed one, not the duplicate
    assert body["success"] is True and body["error"] is None
    assert body["meta"]["count"] == 1 and body["meta"]["limit"] == 20 and body["meta"]["request_id"].startswith("req_")
    assert JOB_KEYS <= set(body["data"][0])  # every key it always had; the typed keys came with plan 02-05


def test_without_a_query_every_open_unique_job_is_listed(api_client, index):
    assert _ids(api_client.get("/v1/jobs")) == {index["first"], index["second"]}


def test_the_country_filter_is_case_insensitive(api_client, index):
    assert _ids(api_client.get("/v1/jobs", params={"country": "in"})) == {index["first"]}


def test_remote_means_stated_remote_and_not_remote_means_stated_not_remote(api_client, index):
    assert _ids(api_client.get("/v1/jobs", params={"remote": "true"})) == {index["first"]}
    # The American posting does not say, so it is neither: unknown is never guessed.
    assert _ids(api_client.get("/v1/jobs", params={"remote": "false"})) == set()


def test_stats_count_what_the_index_holds(api_client, index):
    data = api_client.get("/v1/stats").json()["data"]
    assert data["open_postings"] == 3 and data["open_unique_jobs"] == 2 and data["closed_postings"] == 1
    assert data["open_unique_jobs_india"] == 1 and data["open_unique_jobs_remote"] == 1
    assert data["employers_with_open_jobs"] == 1 and data["last_verified_at"] is not None
