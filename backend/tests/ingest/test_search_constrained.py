"""CHT-02, invariant 1, threat T-05-21: a search that applies the caller's hard constraints inside its one SQL statement
(plan 05-03).

search(..., constrained_for=person) adds the test constraint_violations(job, compile_constraints(person)) = '{}' to the
statement that picks the jobs, so ranking and the cursor only ever see jobs that pass, and pages stay full. These tests
use invented people and postings. The property test compares the result with the independent checker of plan 04-02 over
200 postings made by a seeded generator; the route tests go through the real API role, so row-level security is on.
"""

import random
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.ingest.search import CONSTRAINED, InvalidCursor, search
from app.match import checker
from app.platform import sessions
from app.profile import service
from tests.match.factories import make_job, make_user


def candidate(conn, *constraints):
    """A person with these hard constraints (kind, fields, statement), and the transaction acting as them, as the API
    does for a signed-in request."""
    user = make_user(conn)
    for kind, fields, statement in constraints:
        service.set_constraint(conn, user, kind, fields, statement)
    sessions.act_as(conn, user)
    return user


def job(conn, title, **fields):
    """An open invented posting every search for "engineer" finds. Returns its id."""
    return make_job(
        conn, title=title, description=f"{title}. A Python engineer role, invented.", requirements=[], **fields
    ).id


def run(conn, user, q="", *, limit=500, cursor=None, mode="keyword"):
    return search(
        conn, q=q, country=None, city=None, remote=None, limit=limit, cursor=cursor, mode=mode, constrained_for=user
    )


def ids(page) -> set[int]:
    return {row["id"] for row in page.rows}


def everyone(conn, q="") -> set[int]:
    return ids(
        search(conn, q=q, country=None, city=None, remote=None, limit=500, cursor=None, mode="keyword")
    )  # the Phase 2 call, no person


def pages(conn, user, q, limit) -> Iterator[list[int]]:
    cursor = None
    while True:
        page = run(conn, user, q, limit=limit, cursor=cursor)
        yield [row["id"] for row in page.rows]
        cursor = page.next_cursor
        if cursor is None:
            return


# ---- the filter ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("q", ["", "engineer"], ids=["listing", "ranked"])
def test_a_constrained_search_leaves_out_what_the_constraints_forbid_and_keeps_what_they_do_not_say(conn, q):
    ada = candidate(
        conn,
        ("location", {"country_codes": ["IN"]}, "Only jobs in India."),
        ("excluded_company", {"company_names": ["Ashgrove Capital"]}, "Never Ashgrove Capital."),
    )
    inside = job(conn, "Data engineer", country="IN")
    abroad = job(conn, "Platform engineer", country="DE")
    excluded = job(conn, "Staff engineer", country="IN", company_name="Ashgrove Capital Inc")  # same company key
    unstated = job(conn, "Backend engineer")  # the posting states no country: unknown passes, as in every run

    assert everyone(conn, q) == {inside, abroad, excluded, unstated}
    assert ids(run(conn, ada, q)) == {inside, unstated}


def test_a_person_with_no_constraints_gets_the_same_jobs_as_an_anonymous_search(conn):
    bob = candidate(conn)
    some = {job(conn, "Data engineer", country="IN"), job(conn, "Platform engineer", country="DE")}
    assert ids(run(conn, bob)) == some == everyone(conn)


def test_another_persons_constraints_never_apply(conn):
    ada = candidate(conn, ("location", {"country_codes": ["IN"]}, "Only jobs in India."))
    bob = candidate(conn, ("excluded_company", {"company_names": ["Initech Demo"]}, "Never Initech Demo."))
    inside = job(conn, "Data engineer", country="IN", company_name="Corvid Pay")
    abroad = job(conn, "Platform engineer", country="DE", company_name="Corvid Pay")
    initech = job(conn, "Staff engineer", country="IN")

    sessions.act_as(conn, ada)
    assert ids(run(conn, ada)) == {inside, initech}
    sessions.act_as(conn, bob)
    assert ids(run(conn, bob)) == {inside, abroad}


@pytest.mark.parametrize("q", ["", "engineer"], ids=["listing", "ranked"])
def test_paging_through_a_result_set_with_violators_in_it_skips_and_repeats_nothing(conn, q):
    ada = candidate(conn, ("location", {"country_codes": ["IN"]}, "Only jobs in India."))
    kept = []
    for n in range(9):
        kept.append(job(conn, f"Engineer {n}", country="IN"))
        job(conn, f"Engineer abroad {n}", country="DE")  # a violator between every two jobs that pass

    seen = list(pages(conn, ada, q, limit=2))

    flat = [job_id for page in seen for job_id in page]
    assert sorted(flat) == sorted(kept) and len(flat) == len(set(flat))  # every job once, no violator
    assert [len(page) for page in seen] == [2, 2, 2, 2, 1]  # pages are full: the filter ran before the limit


def test_a_cursor_from_an_anonymous_search_is_not_good_for_a_constrained_one(conn):
    ada = candidate(conn, ("location", {"country_codes": ["IN"]}, "Only jobs in India."))
    for n in range(4):
        job(conn, f"Engineer {n}", country="IN")
    anonymous = search(conn, q="", country=None, city=None, remote=None, limit=2, cursor=None, mode="keyword")
    assert anonymous.next_cursor
    with pytest.raises(InvalidCursor):
        run(conn, ada, cursor=anonymous.next_cursor)


def test_the_search_refuses_to_run_for_a_person_the_transaction_does_not_act_as(conn):
    """Row-level security shows a transaction only its own person's constraints. Asked for someone else's, the filter
    would see none and quietly let everything through, so the search refuses instead."""
    ada = candidate(conn, ("location", {"country_codes": ["IN"]}, "Only jobs in India."))
    job(conn, "Data engineer", country="DE")
    bob = make_user(conn)

    sessions.act_as(conn, bob)
    with pytest.raises(ValueError):
        run(conn, ada)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # nobody
    with pytest.raises(ValueError):
        run(conn, ada)
    sessions.act_as(conn, ada)
    assert ids(run(conn, ada)) == set()


def test_the_persons_constraints_are_compiled_once_for_the_statement_not_once_per_job(conn):
    """compile_constraints reads the constraints table. As an uncorrelated subquery the planner evaluates it once; as a
    plain call in the WHERE clause it would run once for every job in the index."""
    ada = candidate(conn, ("location", {"country_codes": ["IN"]}, "Only jobs in India."))
    plan = "\n".join(
        conn.execute(
            text(f"EXPLAIN SELECT j.id FROM jobs j WHERE {CONSTRAINED.replace(':constrained_for', repr(str(ada)))}")
        ).scalars()
    )  # EXPLAIN takes no bound parameters
    assert "InitPlan" in plan


# ---- the independent checker agrees --------------------------------------------------------------------------------

COUNTRIES = [None, "IN", "US", "DE", "GB"]
CITIES = [None, "Pune", "Austin", "Berlin", "Bengaluru"]
ARRANGEMENTS = [(), ("remote",), ("onsite",), ("hybrid",), ("onsite", "hybrid")]
EMPLOYMENT = ["unknown", "full_time", "part_time", "contract"]
SENIORITIES = ["unknown", "mid", "senior", "staff"]
VISA = ["unknown", "offered", "not_offered"]
COMPANIES = ["Initech Demo", "Ashgrove Capital", "Ashgrove Capital Inc", "Corvid Pay", "Brightline Health"]
PAY = [(None, None, None), (1_800_000, 2_400_000, "INR"), (3_500_000, 5_000_000, "INR"), (90_000, 120_000, "USD")]


def invented_posting(rng: random.Random, n: int) -> dict:
    low, high, currency = rng.choice(PAY)
    return {
        "title": f"Engineer {n}",
        "company_name": rng.choice(COMPANIES),
        "country": rng.choice(COUNTRIES),
        "city": rng.choice(CITIES),
        "arrangements": rng.choice(ARRANGEMENTS),
        "employment_type": rng.choice(EMPLOYMENT),
        "seniority": rng.choice(SENIORITIES),
        "salary_min": low,
        "salary_max": high,
        "salary_currency": currency,
        "visa_sponsorship": rng.choice(VISA),
    }


# Three people, each with several kinds of constraint; between them every kind is exercised.
PEOPLE = [
    (
        [
            ("location", {"country_codes": ["IN"], "cities": ["pune", "bengaluru"]}, "India: Pune or Bengaluru."),
            ("remote_policy", {"arrangements": ["remote", "hybrid"]}, "Remote or hybrid."),
            ("salary_floor", {"salary_floor": {"amount": 3_000_000, "currency": "INR"}}, "30 lakh."),
        ],
        checker.Constraints(
            countries=["IN"],
            cities=["pune", "bengaluru"],
            arrangements=["remote", "hybrid"],
            salary_floor=3_000_000,
            salary_currency="INR",
        ),
    ),
    (
        [
            ("work_authorization", {"country_codes": ["US"]}, "Authorised in the US."),
            ("employment_type", {"employment_types": ["full_time"]}, "Full time."),
            ("seniority", {"seniorities": ["senior", "staff"]}, "Senior or staff."),
        ],
        checker.Constraints(
            authorized_countries=["US"], employment_types=["full_time"], seniorities=["senior", "staff"]
        ),
    ),
    (
        [("excluded_company", {"company_names": ["Ashgrove Capital", "Corvid Pay"]}, "Never those two.")],
        checker.Constraints(
            excluded_company_keys=[checker.company_key("Ashgrove Capital"), checker.company_key("Corvid Pay")]
        ),
    ),
]


def facts_of(conn, job_id: int) -> checker.JobFacts:
    row = conn.execute(
        text(
            """SELECT company_name, country, city, work_arrangements::text[] AS arrangements,
                      employment_type::text AS employment_type, seniority::text AS seniority, salary_min, salary_max,
                      salary_currency, visa_sponsorship::text AS visa FROM jobs WHERE id = :id"""
        ),
        {"id": job_id},
    ).one()
    return checker.JobFacts(
        company_name=row.company_name,
        country=row.country,
        city=row.city,
        work_arrangements=tuple(row.arrangements),
        employment_type=row.employment_type,
        seniority=row.seniority,
        salary_min=row.salary_min,
        salary_max=row.salary_max,
        salary_currency=row.salary_currency,
        visa_sponsorship=row.visa,
    )


def test_for_200_generated_postings_the_constrained_search_returns_exactly_the_jobs_the_independent_checker_clears(
    conn,
):
    rng = random.Random(20261123)  # seeded: the same postings every run
    posted = []
    for n in range(200):
        posting = invented_posting(rng, n)
        posted.append(job(conn, posting.pop("title"), **posting))
    facts = {job_id: facts_of(conn, job_id) for job_id in posted}
    assert everyone(conn) == set(posted)

    for constraints, expected in PEOPLE:
        person = candidate(conn, *constraints)
        returned = ids(run(conn, person))
        cleared = {job_id for job_id, found in facts.items() if checker.violations(found, expected) == []}
        assert returned == cleared
        assert 0 < len(returned) < len(posted), (
            "a person whose constraints remove nothing, or everything, proves nothing"
        )


# ---- through the API -----------------------------------------------------------------------------------------------


@pytest.fixture
def world(api_client, engine):
    """Ada (India only), Bob (no constraints), one posting in India and one in Germany, all committed for the API."""
    with engine.begin() as c:
        ada = make_user(c)
        service.set_constraint(c, ada, "location", {"country_codes": ["IN"]}, "Only jobs in India.")
        bob = make_user(c)
        inside = job(c, "Data engineer", country="IN")
        abroad = job(c, "Platform engineer", country="DE")
    return {"ada": ada, "bob": bob, "inside": inside, "abroad": abroad}


def sign_in(client, engine, user) -> None:
    """A real session: the cookie the API checks, not an override of its dependency."""
    with engine.begin() as c:
        client.cookies.set(sessions.COOKIE_NAME, sessions.start(c, user))


def listed(response) -> tuple[set[int], dict]:
    assert response.status_code == 200, response.text
    body = response.json()
    return {row["id"] for row in body["data"]}, body["meta"]


def test_the_jobs_route_needs_a_session_to_apply_constraints(api_client, world):
    response = api_client.get("/v1/jobs", params={"apply_constraints": "true"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_signed_in_search_with_apply_constraints_is_filtered_and_says_so(api_client, engine, world):
    sign_in(api_client, engine, world["ada"])
    found, meta = listed(api_client.get("/v1/jobs", params={"apply_constraints": "true"}))
    assert found == {world["inside"]}
    assert meta["constraints_applied"] is True and meta["count"] == 1


def test_without_the_parameter_the_answer_is_what_phase_2_returned_even_for_a_signed_in_person(
    api_client, engine, world
):
    sign_in(api_client, engine, world["ada"])
    for params in ({}, {"apply_constraints": "false"}):
        found, meta = listed(api_client.get("/v1/jobs", params=params))
        assert found == {world["inside"], world["abroad"]}
        assert "constraints_applied" not in meta


def test_the_person_is_the_session_never_a_request_field(api_client, engine, world):
    sign_in(api_client, engine, world["ada"])
    asked = {"apply_constraints": "true", "user_id": str(world["bob"]), "constrained_for": str(world["bob"])}
    found, _ = listed(api_client.get("/v1/jobs", params=asked))
    assert found == {world["inside"]}  # still Ada's constraints, and Bob's id was not read anywhere

    sign_in(api_client, engine, world["bob"])
    found, meta = listed(api_client.get("/v1/jobs", params={"apply_constraints": "true"}))
    assert found == {world["inside"], world["abroad"]} and meta["constraints_applied"] is True  # Bob has none


def test_constrained_paging_through_the_route_returns_every_passing_job_once(api_client, engine, world):
    with engine.begin() as c:
        more = [job(c, f"Engineer {n}", country="IN") for n in range(5)]
        for n in range(5):
            job(c, f"Engineer abroad {n}", country="DE")
    sign_in(api_client, engine, world["ada"])
    seen, cursor = [], None
    while True:
        params = {"apply_constraints": "true", "limit": 2, "q": "engineer", **({"cursor": cursor} if cursor else {})}
        response = api_client.get("/v1/jobs", params=params)
        assert response.status_code == 200, response.text
        seen += [row["id"] for row in response.json()["data"]]
        cursor = response.json()["meta"]["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted([world["inside"], *more])


def test_the_openapi_document_describes_the_parameter(api_client):
    spec = api_client.get("/openapi.json").json()
    names = {p["name"] for p in spec["paths"]["/v1/jobs"]["get"]["parameters"]}
    assert "apply_constraints" in names
    assert "user_id" not in names  # nothing names a person
