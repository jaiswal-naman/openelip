"""Invariants 1 and 2 at the level of a whole run (plan 04-03, task 2; MAT-01, MAT-03, MAT-05).

Invariant 1: no job that breaks a hard constraint is ever sent to a model, shown, or recorded as anything but filtered.
It has two locks, the filter inside the retrieval SQL and the separately written checker, and the first test below is
written so that it fails when the SQL filter is removed even though the checker would still file the violators: the
model calls would already have been spent on them.

Invariant 2: every verdict shown to a person cites a requirement of that job version, whose span still reads as its
text, and facts that are theirs, confirmed and current, with one verdict per requirement.

The generated people and postings are invented and drawn from a seeded generator, so a failure repeats.
"""

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.match import checker, evidence, service
from app.match.validator import Verdict, VerdictSet
from tests.match.fixtures.run import world

LOCAL_DATE = world.LOCAL_DATE
SEED = 20261123
PEOPLE, POSTINGS = 20, 200
COUNTRIES = ("IN", "DE", "US", "GB", "FR")
CITIES = ("Pune", "Berlin", "Austin", "London")
COMPANIES = ("Alpha Inc", "Beta LLC", "Gamma Ltd", "Delta GmbH", "Epsilon Co", "Zeta Corp", "Eta Pvt", "Theta Limited")
ARRANGEMENTS = ("onsite", "hybrid", "remote")
EMPLOYMENT = ("full_time", "part_time", "contract", "internship", "temporary")
SENIORITIES = ("intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive")


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    world.empty_tables(engine)
    world.no_embedder(monkeypatch)
    yield
    world.empty_tables(engine)


# --- the checker is the second lock -----------------------------------------------------------------------------------


def test_the_checker_re_tests_every_recorded_job_outside_the_filtered_sample(engine, monkeypatch):
    world.use_provider(monkeypatch, "all_met.json")
    user = world.add_person(engine)
    for name in "ABCDEFG":
        world.add_job(engine, f"Role {name}", company_name=f"Company {name}")
    world.add_job(engine, "Role abroad", company_name="Company abroad", country="DE")  # the filtered sample
    tested = []
    real = checker.violations

    def spy(job, constraints):
        tested.append(job.company_name)
        return real(job, constraints)

    monkeypatch.setattr(checker, "violations", spy)

    service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert sorted(tested) == [f"Company {name}" for name in "ABCDEFG"]


def test_a_checker_that_disagrees_files_the_job_as_filtered_raises_an_alert_and_marks_the_run(
    engine, monkeypatch, caplog
):
    world.use_provider(monkeypatch, "all_met.json")
    user = world.add_person(engine)
    world.add_job(engine, "Role A", company_name="Company A")
    flagged = world.add_job(engine, "Role B", company_name="Company B")
    real = checker.violations
    monkeypatch.setattr(
        checker, "violations", lambda job, c: ["location"] if job.company_name == "Company B" else real(job, c)
    )

    with caplog.at_level(logging.ERROR, logger="app.event"):
        result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.filtered, result.checker) == (1, 1, "disagreed")
    (row,) = world.fetch(
        engine,
        """SELECT outcome::text AS outcome, reason_codes::text[] AS reasons, violated_kinds::text[] AS violated,
                  (SELECT count(*) FROM match_evidence e WHERE e.match_decision_id = d.id) AS evidence
             FROM match_decisions d WHERE d.job_id = :j""",
        j=flagged.id,
    )
    assert (row.outcome, row.reasons, row.violated, row.evidence) == ("filtered", ["hard_constraint"], ["location"], 0)
    (run,) = world.fetch(engine, "SELECT status::text AS status, error_code, shown_count FROM match_runs")
    assert (run.status, run.error_code, run.shown_count) == ("completed", "checker_disagreement", 1)
    alerts = [json.loads(r.getMessage()) for r in caplog.records if "match.checker_disagreement" in r.getMessage()]
    assert [(a["level"], a["job_id"]) for a in alerts] == [("error", flagged.id)]


def test_a_constraint_added_while_the_run_scores_files_the_job_without_a_disagreement(engine, monkeypatch):
    world.use_provider(monkeypatch, "all_met.json")
    user = world.add_person(engine)
    world.add_job(engine, "Role A", company_name="Company A")
    excluded = world.add_job(engine, "Role B", company_name="Company B")
    real, added = evidence.score, []

    def score_then_exclude(request, *, principal):
        answer = real(request, principal=principal)
        if not added:  # the person adds a constraint while the run is still scoring
            added.append(True)
            with engine.begin() as c:
                world.add_constraint(c, user, "excluded_company", company_names=["Company B"])
                c.execute(text("UPDATE users SET record_version = record_version + 1 WHERE id = :u"), {"u": user})
        return answer

    monkeypatch.setattr(evidence, "score", score_then_exclude)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.checker) == (1, "agreed")
    (row,) = world.fetch(
        engine,
        "SELECT outcome::text AS outcome, violated_kinds::text[] AS v FROM match_decisions WHERE job_id = :j",
        j=excluded.id,
    )
    assert (row.outcome, row.v) == ("filtered", ["excluded_company"])
    (run,) = world.fetch(engine, "SELECT error_code FROM match_runs")
    assert run.error_code is None


# --- invariant 1, over generated people and postings ---------------------------------------------------------------


def draw_constraints(rng):
    """Kind -> fields, for the database rows and for the checker's own dataclass."""
    chosen = {}
    if rng.random() < 0.6:
        chosen["location"] = {
            "country_codes": rng.sample(COUNTRIES, rng.randint(1, 3)),
            "cities": [c.lower() for c in rng.sample(CITIES, rng.randint(1, 2))] if rng.random() < 0.4 else None,
        }
    if rng.random() < 0.5:
        chosen["remote_policy"] = {"arrangements": rng.sample(ARRANGEMENTS, rng.randint(1, 2))}
    if rng.random() < 0.4:
        chosen["work_authorization"] = {"country_codes": rng.sample(COUNTRIES, rng.randint(1, 2))}
    if rng.random() < 0.4:
        chosen["salary_floor"] = {
            "salary_floor": rng.choice((50_000, 100_000, 150_000)),
            "salary_currency": rng.choice(("USD", "EUR")),
        }
    if rng.random() < 0.4:
        chosen["employment_type"] = {"employment_types": rng.sample(EMPLOYMENT, rng.randint(1, 3))}
    if rng.random() < 0.4:
        chosen["seniority"] = {"seniorities": rng.sample(SENIORITIES, rng.randint(1, 4))}
    if rng.random() < 0.4:
        chosen["excluded_company"] = {"company_names": rng.sample(COMPANIES, rng.randint(1, 3))}
    return chosen


def checker_constraints(chosen):
    """The same constraints as the checker's dataclass, written from the draw and not from the database."""
    location, remote = chosen.get("location"), chosen.get("remote_policy")
    floor, excluded = chosen.get("salary_floor"), chosen.get("excluded_company")
    return checker.Constraints(
        countries=location["country_codes"] if location else None,
        cities=location["cities"] if location else None,
        arrangements=remote["arrangements"] if remote else None,
        authorized_countries=chosen["work_authorization"]["country_codes"] if "work_authorization" in chosen else None,
        salary_floor=floor["salary_floor"] if floor else None,
        salary_currency=floor["salary_currency"] if floor else None,
        employment_types=chosen["employment_type"]["employment_types"] if "employment_type" in chosen else None,
        seniorities=chosen["seniority"]["seniorities"] if "seniority" in chosen else None,
        excluded_company_keys=[checker.company_key(n) for n in excluded["company_names"]] if excluded else None,
    )


def draw_posting(rng, n):
    """The fields of one invented posting: a company written three ways, and a stated place, pay, level and kind of
    work only some of the time."""
    company = rng.choice(COMPANIES)
    pay = rng.random() < 0.5
    low = rng.choice((40_000, 90_000, 140_000))
    return {
        "name": f"Role {n:03d}",
        "company_name": rng.choice((company, company.upper(), company.replace(" ", ", ", 1) + ".")),
        "country": rng.choice((*COUNTRIES, None)),
        "city": rng.choice((*CITIES, None)),
        "arrangements": tuple(rng.sample(ARRANGEMENTS, rng.randint(0, 3))),
        "employment_type": rng.choice((*EMPLOYMENT, "unknown")),
        "seniority": rng.choice((*SENIORITIES, "unknown")),
        "salary_min": low if pay else None,
        "salary_max": low + rng.choice((0, 20_000, 60_000)) if pay else None,
        "salary_currency": rng.choice(("USD", "EUR", "GBP")) if pay else None,
        "visa_sponsorship": rng.choice(("offered", "not_offered", "unknown")),
    }


def job_facts(posting):
    return checker.JobFacts(
        company_name=posting["company_name"],
        country=posting["country"],
        city=posting["city"],
        work_arrangements=posting["arrangements"],
        employment_type=posting["employment_type"],
        seniority=posting["seniority"],
        salary_min=posting["salary_min"],
        salary_max=posting["salary_max"],
        salary_currency=posting["salary_currency"],
        visa_sponsorship=posting["visa_sponsorship"],
    )


def test_no_job_that_breaks_a_constraint_is_scored_shown_or_recorded_as_anything_but_filtered(engine, monkeypatch):
    world.use_provider(monkeypatch)
    rng = random.Random(SEED)
    postings = {}
    for n in range(POSTINGS):
        draw = draw_posting(rng, n)
        postings[world.add_job(engine, **draw).id] = job_facts(draw)
    wanted = {}
    for _ in range(PEOPLE):
        chosen = draw_constraints(rng)
        user = world.add_person(engine, constraints=False)
        with engine.begin() as c:
            for kind, fields in chosen.items():
                world.add_constraint(c, user, kind, **fields)
        wanted[user] = checker_constraints(chosen)
    job_of_requirement = {r.id: r.job_id for r in world.fetch(engine, "SELECT id, job_id FROM job_requirements")}
    sent = []

    def spy(request, *, principal):
        """What a model would be sent: record the job, and answer that nothing is evidenced (a valid answer)."""
        sent.append((principal, job_of_requirement[next(iter(request.requirements.values())).id]))
        return VerdictSet(
            verdicts=[Verdict(requirement=a, verdict="no_evidence", facts=[]) for a in request.requirements]
        )

    monkeypatch.setattr(evidence, "score", spy)

    for user in wanted:
        service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    def violations(user, job_id):
        (sql,) = world.fetch(
            engine,
            "SELECT constraint_violations(j, compile_constraints(:u))::text[] AS kinds FROM jobs j WHERE j.id = :j",
            u=user,
            j=job_id,
        )
        return sql.kinds, checker.violations(postings[job_id], wanted[user])

    assert len(sent) > 200, "the generated world must send plenty of jobs to the model for this test to mean anything"
    for user, job_id in sent:
        assert violations(user, job_id) == ([], []), f"a violating job was sent to the model: job {job_id}"
    recorded = world.fetch(
        engine, "SELECT user_id, job_id, outcome::text AS outcome, violated_kinds::text[] AS kinds FROM match_decisions"
    )
    assert any(d.outcome == "filtered" for d in recorded), "the generated constraints must filter some jobs"
    for decision in recorded:
        sql_kinds, checker_kinds = violations(decision.user_id, decision.job_id)
        if decision.outcome == "filtered":
            assert decision.kinds == sql_kinds == checker_kinds != []  # the two implementations name the same kinds
        else:
            assert (sql_kinds, checker_kinds) == ([], []), f"a violating job is recorded as {decision.outcome}"
    assert world.fetch(engine, "SELECT 1 FROM match_runs WHERE error_code IS NOT NULL") == []  # no disagreement


# --- invariant 2 ------------------------------------------------------------------------------------------------------


def test_every_verdict_shown_cites_this_jobs_requirements_and_this_persons_current_facts(engine, monkeypatch):
    world.use_provider(monkeypatch, "all_met.json")
    users = [world.add_person(engine) for _ in range(2)]
    for name in "ABCD":
        world.add_job(engine, f"Role {name}")
    for user in users:
        service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    shown = world.fetch(
        engine,
        """SELECT d.id, d.user_id, d.job_id, d.job_content_version, j.description, j.content_version,
                  (SELECT count(*) FROM job_requirements r
                    WHERE r.job_id = d.job_id AND r.content_version = d.job_content_version) AS required
             FROM match_decisions d JOIN jobs j ON j.id = d.job_id WHERE d.outcome = 'shown'""",
    )
    assert len(shown) == 8
    for decision in shown:
        evidence_rows = world.fetch(
            engine,
            """SELECT e.fact_ids, e.verdict::text AS verdict, r.job_id, r.content_version, r.text,
                      lower(r.span) AS span_start, upper(r.span) AS span_end
                 FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
                WHERE e.match_decision_id = :d""",
            d=decision.id,
        )
        assert len(evidence_rows) == decision.required == 2  # one verdict per requirement, no more and no fewer
        for row in evidence_rows:
            assert (row.job_id, row.content_version) == (decision.job_id, decision.job_content_version)
            assert decision.job_content_version == decision.content_version
            assert decision.description[row.span_start : row.span_end] == row.text  # code points, as the web slices
            assert row.verdict == "no_evidence" or row.fact_ids
            usable = world.fetch(
                engine,
                """SELECT id FROM profile_facts WHERE id = ANY(:ids) AND user_id = :u AND confirmed_at IS NOT NULL
                      AND rejected_at IS NULL AND superseded_at IS NULL""",
                ids=row.fact_ids,
                u=decision.user_id,
            )
            assert len(usable) == len(row.fact_ids)


# --- two runs at once (T-04-17) ------------------------------------------------------------------------------------


def test_two_runs_at_once_never_show_a_job_twice_or_more_than_five_a_day(engine, monkeypatch):
    """Each run decides while holding the person's lock, so the second one sees what the first wrote. Without the lock
    both would pick the same jobs and positions and the database would refuse the second, failing its run. The first
    decision is slowed down so the two overlap."""
    world.use_provider(monkeypatch)
    user = world.add_person(engine)
    for name in "ABCDEFG":
        world.add_job(engine, f"Role {name}")
    real_decide = service._decide

    def slow_decide(*arguments):
        decided = real_decide(*arguments)
        time.sleep(0.5)
        return decided

    monkeypatch.setattr(service, "_decide", slow_decide)
    monkeypatch.setattr(
        evidence,
        "score",
        lambda request, *, principal: VerdictSet(
            verdicts=[Verdict(requirement=alias, verdict="met", facts=["f2"]) for alias in request.requirements]
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.run, user, trigger=t, local_date=LOCAL_DATE) for t in ("onboarding", "on_demand")
        ]
        results = [future.result(timeout=60) for future in futures]

    shown = world.fetch(engine, "SELECT job_id, rank_position FROM match_decisions WHERE outcome = 'shown'")
    assert sum(r.shown for r in results) == len(shown) == 5
    assert sorted(d.rank_position for d in shown) == [1, 2, 3, 4, 5]
    assert len({d.job_id for d in shown}) == 5
    assert world.fetch(engine, "SELECT 1 FROM match_runs WHERE status <> 'completed'") == []
