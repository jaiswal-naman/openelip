"""Plan 06-03: employer roles published as unlisted jobs on a `direct` source (ingest.api.publish_direct_job).

Everything runs in the `conn` fixture's transaction as the worker role, the one that may write the index. Organizations,
roles and the text are invented.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from app.ingest import api, jobs
from app.ingest.api import DirectRequirement
from tests.hiring import world
from tests.hiring.factories import make_org

NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
DESCRIPTION = "Halden Demo needs a data engineer.\nPython is required.\nSQL is required.\nAirflow is a plus."
QUOTES = [("Python is required.", "required"), ("SQL is required.", "required"), ("Airflow is a plus.", "preferred")]


def items(description: str = DESCRIPTION) -> list[DirectRequirement]:
    found = []
    for ordinal, (quote, importance) in enumerate(QUOTES, start=1):
        start = description.index(quote)
        found.append(DirectRequirement(ordinal, importance, "skill", quote, start, start + len(quote)))
    return found


def publish(conn, org, role_id, **over):
    fields = {
        "organization_id": org.id,
        "company_name": org.name,
        "role_id": role_id,
        "title": "Data engineer",
        "description": DESCRIPTION,
        "country": "IN",
        "city": "Pune",
        "work_arrangements": ["hybrid"],
        "employment_type": "full_time",
        "seniority": "senior",
        "salary_min": 80_000,
        "salary_max": 120_000,
        "salary_currency": "USD",
        "visa_sponsorship": "unknown",
        "requirements": items(),
        **over,
    }
    return api.publish_direct_job(conn, **fields)


def test_a_role_becomes_one_unlisted_open_job_on_the_organizations_direct_source(conn):
    org = make_org(conn)
    role_id = uuid4()
    world.act_as(conn, "app_worker", org.owner)

    job_id = publish(conn, org, role_id)

    job = conn.execute(
        text("SELECT *, CAST(work_arrangements AS text[]) AS arrangements FROM jobs WHERE id = :id"), {"id": job_id}
    ).one()
    assert (job.is_listed, job.status, job.content_version, job.requirements_version) == (False, "open", 1, 1)
    assert (job.external_id, job.dedupe_key, job.company_name) == (str(role_id), f"direct:{role_id}", org.name)
    assert (job.salary_min, job.salary_max, job.salary_currency) == (80_000, 120_000, "USD")
    assert (job.country, job.city, job.arrangements, job.seniority) == ("IN", "Pune", ["hybrid"], "senior")
    assert job.apply_url.endswith("/app/today")
    source = conn.execute(text("SELECT * FROM job_sources WHERE id = :id"), {"id": job.source_id}).one()
    assert (source.ats, source.token, source.organization_id, source.is_active) == (
        "direct",
        str(org.id),
        org.id,
        False,
    )
    rows = conn.execute(
        text(
            "SELECT ordinal, importance, text, lower(span) AS s, upper(span) AS e, prompt_version "
            "FROM job_requirements WHERE job_id = :id ORDER BY ordinal"
        ),
        {"id": job_id},
    ).all()
    assert [(r.ordinal, r.importance, r.text) for r in rows] == [(n, i, q) for n, (q, i) in enumerate(QUOTES, 1)]
    assert all(DESCRIPTION[r.s : r.e] == r.text and r.prompt_version == "employer-confirmed" for r in rows)


def test_the_same_organization_reuses_its_source_and_another_organization_gets_its_own(conn):
    first, second = make_org(conn), make_org(conn, "Brightwell Demo")
    world.act_as(conn, "app_worker", first.owner)

    one = publish(conn, first, uuid4())
    two = publish(conn, first, uuid4())
    other = publish(conn, second, uuid4())

    source = {
        job: conn.execute(text("SELECT source_id FROM jobs WHERE id = :id"), {"id": job}).scalar_one()
        for job in (one, two, other)
    }
    assert source[one] == source[two] != source[other]
    assert world.count(conn, "job_sources WHERE ats = 'direct'") == 2


def test_the_fetch_selection_skips_the_direct_source_and_search_never_returns_the_job(conn):
    org = make_org(conn)
    active = conn.execute(
        text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'acme', 'Acme') RETURNING id")
    ).scalar_one()
    world.act_as(conn, "app_worker", org.owner)
    job_id = publish(conn, org, uuid4())

    assert jobs.queue_fetches(conn, NOW) == 1  # only the lever source
    queued = conn.execute(text("SELECT payload FROM queue_jobs WHERE kind = 'ingest.fetch'")).scalars().all()
    assert [p["source_id"] for p in queued] == [active]
    assert api.get_job(conn, job_id) is None
    page = api.search(conn, q="engineer", country=None, city=None, remote=None, limit=20, cursor=None, mode="keyword")
    assert job_id not in [row["id"] for row in page.rows]


def test_closing_sets_the_job_closed_with_its_time_and_twice_is_harmless(conn):
    org = make_org(conn)
    world.act_as(conn, "app_worker", org.owner)
    job_id = publish(conn, org, uuid4())

    api.close_direct_job(conn, job_id)
    api.close_direct_job(conn, job_id)

    row = conn.execute(text("SELECT status, closed_at FROM jobs WHERE id = :id"), {"id": job_id}).one()
    assert row.status == "closed" and row.closed_at is not None
