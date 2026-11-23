"""Plan 06-03: employer roles published as unlisted jobs on a `direct` source (ingest.api.publish_direct_job).

Everything runs in the `conn` fixture's transaction as the API login, the one that publishes: the rows are written by
direct_job_open and direct_job_close, which take the job's fields from the role and refuse anyone who is not a member of
its organization (db/sql/p6_0601_publish_close.sql). Organizations, roles and the text are invented.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.ingest import api, jobs
from app.ingest.api import DirectRequirement
from tests.hiring import world
from tests.hiring.factories import make_org, make_role

NOW = datetime(2030, 1, 1, 10, 30, tzinfo=timezone.utc)
DESCRIPTION = "Halden Demo needs a data engineer.\nPython is required.\nSQL is required.\nAirflow is a plus."
QUOTES = [("Python is required.", "required"), ("SQL is required.", "required"), ("Airflow is a plus.", "preferred")]


def items(description: str = DESCRIPTION) -> list[DirectRequirement]:
    found = []
    for ordinal, (quote, importance) in enumerate(QUOTES, start=1):
        start = description.index(quote)
        found.append(DirectRequirement(ordinal, importance, "skill", quote, start, start + len(quote)))
    return found


def draft(conn, org):
    """A ready draft with the fields its job must copy. Made before act_as, which drops the superuser's privileges."""
    return make_role(
        conn,
        org,
        description=DESCRIPTION,
        requirements=QUOTES,
        pay=(80_000, 120_000, "USD"),
        country="IN",
        city="Pune",
        arrangements=["hybrid"],
        employment_type="full_time",
        seniority="senior",
        confirmed=True,
    )


def link(conn, role, job_id):
    """What the hiring desk's publish does next, as the same API login: the role points at its job."""
    conn.execute(text("UPDATE roles SET job_id = :job WHERE id = :id"), {"job": job_id, "id": role.id})


def publish(conn, role):
    return api.publish_direct_job(
        conn, role_id=role.id, title=role.title, description=DESCRIPTION, requirements=items()
    )


def test_a_role_becomes_one_unlisted_open_job_on_the_organizations_direct_source(conn):
    org = make_org(conn)
    role = draft(conn, org)
    role_id = role.id
    world.act_as(conn, "app_api", org.owner)

    job_id = publish(conn, role)

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
    drafts = draft(conn, first), draft(conn, first), draft(conn, second)
    world.act_as(conn, "app_api", first.owner)
    one = publish(conn, drafts[0])
    two = publish(conn, drafts[1])
    world.act_as(conn, "app_api", second.owner)
    other = publish(conn, drafts[2])

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
    role = draft(conn, org)
    world.act_as(conn, "app_api", org.owner)
    job_id = publish(conn, role)
    world.act_as(conn, "app_worker")  # the fetch sweep runs in the worker

    assert jobs.queue_fetches(conn, NOW) == 1  # only the lever source
    queued = conn.execute(text("SELECT payload FROM queue_jobs WHERE kind = 'ingest.fetch'")).scalars().all()
    assert [p["source_id"] for p in queued] == [active]
    assert api.get_job(conn, job_id) is None
    page = api.search(conn, q="engineer", country=None, city=None, remote=None, limit=20, cursor=None, mode="keyword")
    assert job_id not in [row["id"] for row in page.rows]


def test_closing_sets_the_job_closed_with_its_time_and_twice_is_harmless(conn):
    org = make_org(conn)
    role = draft(conn, org)
    world.act_as(conn, "app_api", org.owner)
    job_id = publish(conn, role)
    link(conn, role, job_id)

    api.close_direct_job(conn, job_id)
    api.close_direct_job(conn, job_id)

    row = conn.execute(text("SELECT status, closed_at FROM jobs WHERE id = :id"), {"id": job_id}).one()
    assert row.status == "closed" and row.closed_at is not None


@pytest.mark.parametrize("who", ["outsider", "agent"])
def test_opening_a_job_for_a_role_is_refused_to_a_caller_outside_its_organization_and_to_an_agent_token(conn, who):
    org = make_org(conn)
    role = draft(conn, org)
    if who == "outsider":
        world.act_as(conn, "app_api", make_org(conn, "Brightwell Demo").owner)
    else:
        world.act_as(conn, "app_api", org.owner, token=uuid4())

    with pytest.raises(DBAPIError, match="not a member of the organization"):  # raised before any write
        publish(conn, role)


def test_a_role_that_already_has_its_job_gets_no_second_one(conn):
    org = make_org(conn)
    role = draft(conn, org)
    world.act_as(conn, "app_api", org.owner)
    link(conn, role, job_id=publish(conn, role))

    with pytest.raises(DBAPIError, match="cannot get a new job"):
        publish(conn, role)
