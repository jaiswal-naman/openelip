"""/v1/jobs and /v1/stats keep their shape after the job tables were converted (API.md 2.4).

The rule under test: is_remote is no longer stored. It is read off work_arrangements: true when the array holds remote,
null when it is empty (the posting does not say), false otherwise. The remote filter and the remote count follow it.
"""

import pytest
from sqlalchemy import text

JOB_KEYS = {
    "id", "title", "company_name", "location_raw", "country", "city", "is_remote", "apply_url", "posted_at",
    "last_verified_at", "content_version",
}  # fmt: skip
STATS_KEYS = {
    "open_postings", "open_unique_jobs", "open_unique_jobs_india", "open_unique_jobs_remote", "closed_postings",
    "employers_with_open_jobs", "last_verified_at",
}  # fmt: skip

# title -> (work_arrangements as stored, is_remote as the API must say it)
ARRANGEMENTS = {
    "Remote only": ("{remote}", True),
    "Remote or hybrid": ("{remote,hybrid}", True),
    "Onsite only": ("{onsite}", False),
    "Onsite or hybrid": ("{onsite,hybrid}", False),
    "Does not say": ("{}", None),
}


@pytest.fixture
def index(engine, api_client):
    with engine.begin() as c:
        source = c.execute(
            text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
        ).scalar_one()
        for n, (title, (stored, _)) in enumerate(ARRANGEMENTS.items()):
            c.execute(
                text(
                    "INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, "
                    "content_hash, dedupe_key, work_arrangements) VALUES (:s, :e, :t, 'Acme', 'Build and run things', "
                    "'https://example.test/apply', 'h', :e, CAST(:w AS work_arrangement[]))"
                ),
                {"s": source, "e": f"job-{n}", "t": title, "w": stored},
            )


def titles(response):
    assert response.status_code == 200
    return {job["title"] for job in response.json()["data"]}


def test_a_job_has_the_keys_it_had_before_and_is_remote_follows_the_array(api_client, index):
    jobs = api_client.get("/v1/jobs").json()["data"]
    assert jobs and all(JOB_KEYS <= set(job) for job in jobs)  # the typed keys came with plan 02-05
    assert {job["title"]: job["is_remote"] for job in jobs} == {
        title: said for title, (_, said) in ARRANGEMENTS.items()
    }


def test_the_remote_filter_applies_the_same_rule(api_client, index):
    assert titles(api_client.get("/v1/jobs", params={"remote": "true"})) == {"Remote only", "Remote or hybrid"}
    # Not remote means the posting says so. A posting that does not say is in neither list.
    assert titles(api_client.get("/v1/jobs", params={"remote": "false"})) == {"Onsite only", "Onsite or hybrid"}
    assert titles(api_client.get("/v1/jobs")) == set(ARRANGEMENTS)


def test_stats_keep_their_seven_keys_and_count_remote_from_the_array(api_client, index):
    data = api_client.get("/v1/stats").json()["data"]
    assert set(data) == STATS_KEYS
    assert data["open_postings"] == 5 and data["open_unique_jobs_remote"] == 2
