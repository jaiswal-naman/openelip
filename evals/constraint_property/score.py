"""The constraint_property suite (plan 04-04; MAT-01, AI_SYSTEM 12.8, threat T-04-23): the SQL constraint filter and the
independent Python checker must give identical answers on every generated pair of one job and one constraint set.

The Phase 1 runner calls run() (this suite is an OwnScorer entry in backend/app/evals/runner.py), so a run is one
eval_runs row with one eval_items row per constraint set, gated like any other suite:

    cd backend && uv run python -m app.evals run constraint_property
    cd backend && uv run python -m app.evals gate constraint_property --run <the id printed last>

No model is called. evaluate() writes the generated jobs and people to the database as the migration login (which
bypasses row-level security, like the test fixtures), asks the database `constraint_violations` and `unverified_fields`
for every pair in one statement, and compares each answer with app.match.checker, whose constraints are built from
profile.api.compiled_constraints the way a run builds them. run() does that inside a transaction it rolls back, on a
scratch database only. README.md holds the pre-registered bars.
"""

import importlib.util
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.evals import scratch
from app.evals.runner import Result, Scored
from app.match import checker
from app.platform import db
from app.profile import api as profile

HERE = Path(__file__).parent


def _load(name: str, path: Path) -> Any:
    """A script next to this one, by path (the runner loads this script by path too, so nothing here is importable by
    name). Loaded once: a test may patch the module, and the runner loads this script again for every run."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cases = _load("constraint_property_cases", HERE / "cases.py")

SUITE = "constraint_property"
SEED = cases.SEED
TASK = "match.constraint_property"  # the runner names a task for every suite; this one never calls a model
RECORDED = HERE / "cases.py"  # and a recording: here it is the seeded generator, which is what a run replays
CONFIG = {"seed": SEED, "generator": "cases.py", "model_calls": 0}
MIN_CASES = 10_000  # the bar of MAT-01 and of README.md "Pre-registered bars"
FLAGS = (
    "location_not_stated",
    "arrangement_not_stated",
    "arrangement_ambiguous",
    "salary_not_stated",
    "salary_currency_differs",
    "employment_type_not_stated",
    "seniority_not_stated",
)
# The flag that says a constraint's field is not stated in the posting (DATA_MODEL 4.1, unverified_fields). A company is
# always stated, so the exclusion has none.
UNSTATED_FLAG = {
    "location": "location_not_stated",
    "remote_policy": "arrangement_not_stated",
    "work_authorization": "location_not_stated",
    "salary_floor": "salary_not_stated",
    "employment_type": "employment_type_not_stated",
    "seniority": "seniority_not_stated",
}
TYPED_COLUMNS = ("country_codes", "cities", "arrangements", "employment_types", "seniorities", "company_names")
JOB_FIELDS = (
    "company_name",
    "country",
    "city",
    "work_arrangements",
    "employment_type",
    "seniority",
    "salary_min",
    "salary_max",
    "salary_currency",
    "visa_sponsorship",
)

INSERT_SOURCE = text(
    """INSERT INTO job_sources (ats, token, company_name)
       VALUES ('lever', 'constraint-property', 'Invented board') RETURNING id"""
)
INSERT_JOB = text(
    """INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements, employment_type,
                        seniority, salary_min, salary_max, salary_currency, visa_sponsorship, description, apply_url,
                        content_hash, dedupe_key)
       VALUES (:source, :key, 'Invented posting', :company_name, :country, :city,
               CAST(:work_arrangements AS work_arrangement[]), CAST(:employment_type AS employment_type),
               CAST(:seniority AS seniority), :salary_min, :salary_max, :salary_currency,
               CAST(:visa_sponsorship AS visa_sponsorship), 'Invented for the constraint property suite.',
               'https://example.test/apply/' || :key, :key, :key)
       RETURNING id"""
)
INSERT_USER = text("INSERT INTO users (email) VALUES (:email) RETURNING id")
INSERT_EVENT = text(  # the change-log row every constraint points at, as plan 04-01's test factory writes it
    """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
       VALUES (:user, 'applied', 'constraint', 'create', CAST(:after AS jsonb), 'candidate_edit', 1) RETURNING id"""
)
INSERT_CONSTRAINT = text(
    """INSERT INTO constraints (user_id, kind, statement, country_codes, cities, arrangements, employment_types,
                               seniorities, salary_floor, salary_currency, company_names, source, profile_event_id)
       VALUES (:user, CAST(:kind AS constraint_kind), :statement, CAST(:country_codes AS char(2)[]),
               CAST(:cities AS text[]), CAST(:arrangements AS work_arrangement[]),
               CAST(:employment_types AS employment_type[]), CAST(:seniorities AS seniority[]), :salary_floor,
               :salary_currency, CAST(:company_names AS text[]), 'candidate_edit', :event)"""
)
# MATERIALIZED: each person's constraints are compiled once, not once for every job.
PAIRS = text(
    """WITH c AS MATERIALIZED (SELECT u.id AS user_id, compile_constraints(u.id) AS cc FROM users u
                              WHERE u.id = ANY(:users))
       SELECT j.id AS job_id, c.user_id, CAST(constraint_violations(j, c.cc) AS text[]) AS violations,
              CAST(unverified_fields(j, c.cc) AS text[]) AS unverified
       FROM jobs j CROSS JOIN c WHERE j.id = ANY(:jobs)"""
)


@dataclass(frozen=True)
class Disagreement:
    """One pair on which the SQL function and the checker gave different lists, with the inputs that produced them."""

    field: str  # "violations" or "unverified"
    job: dict[str, Any]
    constraint_set: dict[str, Any]
    sql: list[str]
    checker: list[str]


@dataclass
class Evaluation:
    cases: int  # pairs compared
    jobs: int
    constraint_sets: list[str]  # the keys of the sets, in order
    disagreements: list[Disagreement]
    coverage: dict[str, dict[str, int]]  # per kind: pairs violated, satisfied on a stated field, left unstated
    flag_counts: dict[str, int]  # per unverified flag: pairs the database gave it to


def job_facts(job: dict[str, Any]) -> checker.JobFacts:
    return checker.JobFacts(**{name: job[name] for name in JOB_FIELDS})


def constraints_of(compiled: dict[str, dict[str, Any]]) -> checker.Constraints:
    """profile.api.compiled_constraints (by kind; a kind never set is absent) in the fields the checker reads."""
    floor = compiled.get("salary_floor", {}).get("salary_floor")
    names = compiled.get("excluded_company", {}).get("company_names")
    return checker.Constraints(
        countries=compiled.get("location", {}).get("country_codes"),
        cities=compiled.get("location", {}).get("cities"),
        arrangements=compiled.get("remote_policy", {}).get("arrangements"),
        authorized_countries=compiled.get("work_authorization", {}).get("country_codes"),
        salary_floor=floor["amount"] if floor else None,
        salary_currency=floor["currency"] if floor else None,
        employment_types=compiled.get("employment_type", {}).get("employment_types"),
        seniorities=compiled.get("seniority", {}).get("seniorities"),
        excluded_company_keys=[checker.company_key(name) for name in names] if names else None,
    )


def _columns(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    """The typed columns of a constraints row for one kind: what the kind does not use is NULL."""
    row: dict[str, Any] = {**dict.fromkeys(TYPED_COLUMNS), "salary_floor": None, "salary_currency": None}
    if kind == "salary_floor":
        row["salary_floor"], row["salary_currency"] = value["salary_floor"]["amount"], value["salary_floor"]["currency"]
    else:
        row.update(value)
    return row


def insert_cases(conn: Connection, generated: Any) -> tuple[dict[int, dict[str, Any]], dict[UUID, dict[str, Any]]]:
    """Write the jobs and the people (each with the constraints of one set) as the connection's login; returns the jobs
    by id and the sets by the id of the person made for each."""
    source = conn.execute(INSERT_SOURCE).scalar_one()
    jobs: dict[int, dict[str, Any]] = {}
    for job in generated.jobs:
        params = {name: job[name] for name in JOB_FIELDS} | {"source": source, "key": job["key"]}
        jobs[conn.execute(INSERT_JOB, params).scalar_one()] = job
    people: dict[UUID, dict[str, Any]] = {}
    for cset in generated.constraint_sets:
        user = conn.execute(INSERT_USER, {"email": f"{cset['key']}@example.test"}).scalar_one()
        for kind, value in cset["constraints"].items():
            event = conn.execute(INSERT_EVENT, {"user": user, "after": json.dumps({"kind": kind})}).scalar_one()
            row = {"user": user, "kind": kind, "statement": f"Invented {kind} constraint.", "event": event}
            conn.execute(INSERT_CONSTRAINT, row | _columns(kind, value))
        people[user] = cset
    return jobs, people


def evaluate(conn: Connection, generated: Any) -> Evaluation:
    """Compare the database's answers with the checker's for every pair of the cases. Leaves the invented rows on the
    connection: the caller rolls them back."""
    jobs, people = insert_cases(conn, generated)
    constraints = {user: constraints_of(profile.compiled_constraints(conn, user)) for user in people}
    coverage = {kind: {"violated": 0, "passed": 0, "unstated": 0} for kind in cases.KINDS}
    flag_counts = dict.fromkeys(FLAGS, 0)
    disagreements: list[Disagreement] = []
    pairs = 0
    for row in conn.execute(PAIRS, {"users": list(people), "jobs": list(jobs)}):
        pairs += 1
        job, cset = jobs[row.job_id], people[row.user_id]
        facts, wanted = job_facts(job), constraints[row.user_id]
        answers = (
            ("violations", list(row.violations), checker.violations(facts, wanted)),
            ("unverified", list(row.unverified), checker.unverified(facts, wanted)),
        )
        for field, sql, python in answers:
            if sql != python:
                disagreements.append(Disagreement(field, job, cset, sql, python))
        for flag in row.unverified:
            flag_counts[flag] += 1
        for kind in cset["constraints"]:
            if kind in row.violations:
                coverage[kind]["violated"] += 1
            elif UNSTATED_FLAG.get(kind) in row.unverified:
                coverage[kind]["unstated"] += 1
            else:
                coverage[kind]["passed"] += 1
    return Evaluation(pairs, len(jobs), [s["key"] for s in generated.constraint_sets], disagreements, coverage, flag_counts)


def format_disagreements(evaluation: Evaluation, limit: int = 10) -> str:
    """The first disagreements, one to a line, with both answers and the inputs that produced them."""
    shown = evaluation.disagreements[:limit]
    lines = [f"{len(evaluation.disagreements)} disagreements in {evaluation.cases} pairs; the first {len(shown)}:"]
    for d in shown:
        lines.append(
            f"{d.job['key']} x {d.constraint_set['key']} {d.field}: SQL {d.sql} checker {d.checker} "
            f"job={json.dumps(d.job, ensure_ascii=False)} "
            f"constraints={json.dumps(d.constraint_set['constraints'], ensure_ascii=False)}"
        )
    return "\n".join(lines)


def scored(evaluation: Evaluation) -> Scored:
    """One item per constraint set (it passes when no job disagrees on it) and the run's metrics. score is 1.0 only when
    both pre-registered bars hold: that is what the gate reads."""
    by_set: dict[str, list[Disagreement]] = defaultdict(list)
    for d in evaluation.disagreements:
        by_set[d.constraint_set["key"]].append(d)
    results = [
        Result(
            key,
            None,
            {"disagreements": 0},
            {
                "disagreements": len(by_set[key]),
                "first": [  # the first few, with their inputs, so a failure can be reproduced from the record
                    {
                        "field": d.field,
                        "job": d.job,
                        "constraint_set": d.constraint_set["constraints"],
                        "sql": d.sql,
                        "checker": d.checker,
                    }
                    for d in by_set[key][:3]
                ],
            },
            not by_set[key],
        )
        for key in evaluation.constraint_sets
    ]
    by_field = Counter(d.field for d in evaluation.disagreements)
    pairs_that_disagree = len({(d.job["key"], d.constraint_set["key"]) for d in evaluation.disagreements})
    held = {"constraint_violations": pairs_that_disagree == 0, "cases": evaluation.cases >= MIN_CASES}
    metrics = {
        "score": 1.0 if all(held.values()) else 0.0,
        "constraint_violations": pairs_that_disagree,  # pairs on which either list differs: the bar is zero
        "cases": evaluation.cases,
        "jobs": evaluation.jobs,
        "constraint_sets": len(evaluation.constraint_sets),
        "disagreeing_violations": by_field["violations"],
        "disagreeing_unverified": by_field["unverified"],
        "bars": held,
        "bars_met": all(held.values()),
        "coverage": evaluation.coverage,
        "flag_counts": evaluation.flag_counts,
        "seed": SEED,
    }
    return Scored(results, metrics)


def refuse_unless_scratch(conn: Connection) -> None:
    """The run writes invented rows (and rolls them back) through the migration login: only a scratch database may take
    that, the same rule the other scripts that write eval rows keep (app.evals.scratch)."""
    name = conn.execute(text("SELECT current_database()")).scalar_one()
    if not scratch.SCRATCH_NAME.fullmatch(name):
        raise RuntimeError(f"database {name!r} is not a scratch one: a name ending in _test, or 'evals'")


def run(recorded: Path) -> Scored:
    """The runner's entry point. `recorded` is the runner's default for a suite (this generator): there is nothing to
    replay, and a run is the same every time because the generator is seeded."""
    with db.engine("migrate").connect() as conn:
        transaction = conn.begin()
        try:
            refuse_unless_scratch(conn)
            evaluation = evaluate(conn, cases.generate(SEED))
        finally:
            transaction.rollback()
    return scored(evaluation)
