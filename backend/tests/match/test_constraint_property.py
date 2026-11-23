"""MAT-01, invariant 1, threat T-04-23: the SQL constraint filter and the independent Python checker agree on every one
of at least 10,000 generated cases (plan 04-04).

The cases come from evals/constraint_property/cases.py (seeded and invented) and are compared by the same function the
gate runs, evals/constraint_property/score.py, on the rolled-back `conn` fixture: so a test here exercises what CI
exercises. For every pair of one job and one constraint set, SQL constraint_violations and unverified_fields must equal
app.match.checker.violations and unverified, with the checker's constraints built from profile.api.compiled_constraints.
The last tests break the checker on purpose, to show that the suite notices.
"""

import importlib.util
import sys

import pytest

from app.evals import EVALS
from app.match import checker

SUITE_DIR = EVALS / "constraint_property"
KINDS = (
    "location",
    "remote_policy",
    "work_authorization",
    "salary_floor",
    "employment_type",
    "seniority",
    "excluded_company",
)
FLAGS = (
    "location_not_stated",
    "arrangement_not_stated",
    "arrangement_ambiguous",
    "salary_not_stated",
    "salary_currency_differs",
    "employment_type_not_stated",
    "seniority_not_stated",
)
MIN_PER_CLASS = 100  # every kind is violated, satisfied on a stated field and left unstated at least this often


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("constraint_property_score", SUITE_DIR / "score.py")
cases = score.cases
SEEDS = [score.SEED, 1, 2, 3]  # the gate's seed and three more: the generator is deterministic, so these are fixed too


# ---- the generator -----------------------------------------------------------------------------------------------


def test_the_generator_is_seeded_and_makes_at_least_200_jobs_50_constraint_sets_and_10000_pairs():
    first, again, other = cases.generate(score.SEED), cases.generate(score.SEED), cases.generate(1)
    assert first == again and first != other  # the same seed gives the same cases, another seed other ones
    assert len(first.jobs) >= 200 and len(first.constraint_sets) >= 50 and first.pairs >= 10_000
    assert len({job["key"] for job in first.jobs}) == len(first.jobs)
    assert len({s["key"] for s in first.constraint_sets}) == len(first.constraint_sets)


def test_the_constraint_sets_include_none_every_kind_alone_and_every_kind_at_once():
    sets = cases.generate(score.SEED).constraint_sets
    assert sets[0]["constraints"] == {}  # a person who set no hard constraint
    assert [list(s["constraints"]) for s in sets[1 : len(KINDS) + 1]] == [[kind] for kind in KINDS]
    assert list(sets[len(KINDS) + 1]["constraints"]) == list(KINDS)


def test_company_names_include_case_punctuation_and_legal_suffix_variants_near_misses_and_accented_names():
    jobs = cases.generate(score.SEED).jobs
    by_key = {checker.company_key(name): name for name in cases.EMPLOYERS}
    names = [job["company_name"] for job in jobs]
    variants = [n for n in names if checker.company_key(n) in by_key and n != by_key[checker.company_key(n)]]
    near = [
        n
        for n in names
        if checker.company_key(n) not in by_key and any(e.split()[0].lower() in n.lower() for e in by_key.values())
    ]
    assert len(variants) >= 20 and len(near) >= 5
    assert any(n.isupper() for n in variants) and any(n.islower() for n in variants)
    assert any(suffix in n for n in variants for suffix in cases.LEGAL_SUFFIXES)
    assert any("-" in n or "," in n or n.endswith(".") for n in variants)
    assert any(not n.isascii() for n in names)


# ---- the property --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_the_sql_filter_and_the_checker_agree_on_every_pair(conn, seed):
    evaluation = score.evaluate(conn, cases.generate(seed))
    assert evaluation.cases >= 10_000
    assert evaluation.disagreements == [], score.format_disagreements(evaluation)


def test_every_kind_is_violated_satisfied_and_left_unstated_at_least_a_hundred_times(conn):
    evaluation = score.evaluate(conn, cases.generate(score.SEED))
    for kind in KINDS:
        seen = evaluation.coverage[kind]
        assert seen["violated"] >= MIN_PER_CLASS and seen["passed"] >= MIN_PER_CLASS, (kind, seen)
        # a posting always states its company, so the exclusion has no unstated case
        assert (seen["unstated"] == 0) if kind == "excluded_company" else seen["unstated"] >= MIN_PER_CLASS, (
            kind,
            seen,
        )
    for flag in FLAGS:  # including a salary in another currency than the floor's
        assert evaluation.flag_counts[flag] >= MIN_PER_CLASS, flag


# ---- the suite notices a broken checker ----------------------------------------------------------------------------


def test_a_checker_that_ignores_excluded_companies_is_caught_and_the_first_ten_are_printed_with_their_inputs(
    conn, monkeypatch
):
    real = checker.violations
    monkeypatch.setattr(checker, "violations", lambda job, c: [k for k in real(job, c) if k != "excluded_company"])
    evaluation = score.evaluate(conn, cases.generate(score.SEED))
    assert len(evaluation.disagreements) >= 100
    assert {d.field for d in evaluation.disagreements} == {"violations"}
    lines = score.format_disagreements(evaluation).splitlines()
    assert len(lines) == 1 + 10  # a header, then the first ten
    first = evaluation.disagreements[0]
    assert first.job["key"] in lines[1] and first.constraint_set["key"] in lines[1]
    assert "excluded_company" in lines[1] and first.job["company_name"] in lines[1]  # the inputs are printed
    assert score.format_disagreements(evaluation, limit=3).count("\n") == 3


def test_a_checker_that_counts_a_salary_equal_to_the_floor_as_below_it_is_caught_so_the_boundary_is_reached(
    conn, monkeypatch
):
    real = checker.violations

    def off_by_one(job, c):
        found = real(job, c)
        if c.salary_floor == job.salary_max and job.salary_currency == c.salary_currency and job.salary_max is not None:
            return sorted({*found, "salary_floor"}, key=KINDS.index)
        return found

    monkeypatch.setattr(checker, "violations", off_by_one)
    assert score.evaluate(conn, cases.generate(score.SEED)).disagreements


def test_a_checker_that_forgets_a_currency_flag_is_caught_on_the_unverified_flags_alone(conn, monkeypatch):
    real = checker.unverified
    monkeypatch.setattr(
        checker, "unverified", lambda job, c: [f for f in real(job, c) if f != "salary_currency_differs"]
    )
    evaluation = score.evaluate(conn, cases.generate(score.SEED))
    assert evaluation.disagreements and {d.field for d in evaluation.disagreements} == {"unverified"}
