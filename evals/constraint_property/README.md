# Constraint property: the SQL filter and the independent checker must agree on every case

This suite is the property half of MAT-01 ("no job that violates a stated hard constraint is ever shown as a match") and
an **absolute CI blocker** (AI_SYSTEM 12.8, threat T-04-23). The match run removes jobs with one SQL function,
`constraint_violations(job, compile_constraints(person))`, and then asks a second, independent implementation in Python,
`app.match.checker.violations`, about every job it records. The two were written separately from the rules of
DATA_MODEL 4.1. This suite generates thousands of invented jobs and constraint sets and requires the two to give
**identical answers on every pair**: the violated kinds and the unverified flags (the fields a posting leaves unstated).

It needs no model and no key, and runs in the pull-request eval gate.

## Pre-registered bars

Written on 2026-11-23 (AI_SYSTEM 12.1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). No run of this suite existed when these bars were written: this file and the floor in
`evals/thresholds.toml` are the first files of the suite in git history, committed before `cases.py`, `score.py` and any
test.

**What is scored.** A run generates the cases from a fixed seed (`cases.generate`), writes them to a scratch database
(inside a transaction that is rolled back), and for every pair of one job and one constraint set compares, as lists in enum
order:

- the SQL `constraint_violations(j, compile_constraints(u))` with `app.match.checker.violations`, and
- the SQL `unverified_fields(j, compile_constraints(u))` with `app.match.checker.unverified`,

where the checker's constraints are built from `app.profile.api.compiled_constraints`, the way a run builds them.
A pair is a *disagreement* when either list differs.

**The bars** (absolute: AI_SYSTEM 12.8 lists a constraint-property failure as a CI blocker):

- `constraint_violations = 0`: no disagreement, on any pair.
- `cases >= 10000`: at least 10,000 generated pairs (MAT-01).

`gate.py` compares only `metrics.score` with the floor, so `score` is 1.0 when both bars hold and 0.0 otherwise, and
`evals/thresholds.toml` sets `min_score = 1.0`. The pass criterion is therefore "no disagreement and enough cases", and
a single disagreement fails the gate. The metrics also record the number of pairs, jobs and constraint sets, how many
pairs disagreed on each list, and for every kind of constraint how often it was violated, satisfied on a stated field and
left unstated (the generator is checked to reach at least 100 of each by the tests, so a generator that stops testing a
rule cannot pass quietly).

**What a failure means.** Fix the side that contradicts the rule of DATA_MODEL 4.1: the checker if it does, otherwise the
SQL function, which is a migration and needs a new revision rather than an edit.

## Files

| File | What it is |
|---|---|
| `cases.py` | `generate(seed)`: seeded, stdlib `random` only (no `hypothesis`: INDEX decision 13). 240 jobs and 60 constraint sets, 14,400 pairs. |
| `score.py` | The scorer the Phase 1 runner calls (`constraint_property` is an `OwnScorer` entry in `SUITES`): writes the cases, compares, records the run. |

## What the generator covers

Every company, title and place is invented or generic; no real posting or person is used.

- **Constraint sets:** the first set holds no constraint at all, each of the seven kinds comes alone in the next seven (so
  a rule is tested with nothing else to hide behind), one holds all seven, and the rest are random subsets.
- **Every field three ways:** stated and matching, stated and contradicting, and unstated, for the country, city, work
  arrangements, employment type, seniority, salary and visa sponsorship. A job in a remote arrangement is checked against
  city constraints (the cities do not apply to it).
- **Company names** include case, punctuation and legal-suffix variants of the excluded names ("INITECH DEMO", "Initech
  Demo, Inc.", "initech-demo Pvt. Ltd."), near misses that are a different company ("Initech Demos"), and accented names.
- **Salaries** are stated in the floor's currency, in another currency, and not at all; they sit at, just under and just
  over each floor, so a job paying exactly the floor is tested as not below it; only a maximum, only a minimum, or both.
- **Places** include accented cities and the same city in capitals or lower case.

## Commands

```
cd backend

# generate, compare and record the run, then gate it (what CI does; needs a migrated scratch database)
uv run python -m app.evals run constraint_property
uv run python -m app.evals gate constraint_property --run <the id printed last>

# the property over more seeds, in the test suite
uv run pytest tests/match/test_constraint_property.py tests/evals/test_constraint_property_suite.py -q
```

The run writes its invented rows through `DATABASE_URL_MIGRATE` (it bypasses row-level security, like the test fixtures)
and rolls them back, so it leaves only the `eval_runs` row. It refuses any database whose name is not `..._test` or
`evals`, and a recorded run is frozen and cannot be deleted.

## Limits

- **Two implementations of one reading.** The checker was written from DATA_MODEL 4.1 without reading the SQL, so a bug in
  either is caught. A misreading of the rule that both share is not: that is for the maintainer's review of 4.1.
- **A small value space on purpose.** Six countries, a dozen cities, a handful of companies: small spaces hit each rule
  from both sides thousands of times. They do not cover every script, locale or spelling a real posting can have. Only
  Latin accented letters are generated; `company_key` strips every character outside `a-z0-9`, so a name with no Latin
  letters has the empty key in both implementations.
- **The generator is code, not a recording.** The same seed gives the same cases, so a run is repeatable; the tests also
  try other seeds.
