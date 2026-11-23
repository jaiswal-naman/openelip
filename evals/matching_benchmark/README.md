# Matching benchmark: snapshot, invented profiles, pooled labels, six methods

This folder answers EVL-02: how do at least four ways of matching a candidate to jobs compare on quality and cost, on
the same postings, after the same constraint filter (AI_SYSTEM 12.5)? It holds a frozen snapshot of public postings, 20
invented candidate profiles, relevance labels for the jobs the methods return, a runner for six methods, and a report.
The runner needs no model key for the four methods that call no model.

## Read this first: what this benchmark does and does not show

- **The labels are agent-drafted, maintainer review pending.** Every label says
  `labels: "agent-drafted, maintainer review pending"`. One agent wrote the profiles and judged every pooled pair from
  `GUIDELINE.md`; nobody has reviewed them, and no second rater has labelled a sample. Until the maintainer reviews them
  and a second rater labels 20% (Cohen's kappa reported), every number from this folder is a first reading and the
  report must say so.
- **It is small.** AI_SYSTEM 12.2 targets 50 profiles and 1,000 to 2,000 pooled pairs; this set has 20 profiles and
  about 500 pairs. With 20 profiles the paired intervals are wide, and a difference whose interval contains 0 is
  reported as "no detectable difference", never as a win or a loss.
- **The profiles are invented** (`Demo Candidate 01` and so on, addresses on `example.test`). Synthetic data is labelled
  and reported separately (AI_SYSTEM 12.1 rule 7). The postings are real and public.
- **Quality numbers come from one recorded run and are not claims beyond it.** They describe these 20 profiles over
  this snapshot on the day it was exported. They say nothing about other candidates, other postings or other days, and
  nothing about any other product (AI_SYSTEM 12.1 rule 7).
- **The two model-backed methods are not run here.** M5 and M6 are implemented and tested on a small fixture with the
  gateway's fake provider. With no model key they report `not run: needs a model key`. The pre-registered decision
  between them waits for a live run (manual checks, below).

## Files

| File | What it is |
|---|---|
| `README.md` | This file: the methods, the metrics and the rules, written before any run. |
| `GUIDELINE.md` | How a labeller reads a profile and a posting card, grades 0 to 3, worked examples. |
| `export_snapshot.py` | Writes `snapshot.jsonl` and `snapshot.meta.json` from a database at the merged schema, in a read-only session. |
| `snapshot.jsonl`, `snapshot.meta.json` | The frozen postings and where they came from (selection SQL, counts, date, schema revision). |
| `profiles.jsonl` | The 20 invented profiles: dated roles, skills, constraints, preferences. |
| `load.py` | Builds a benchmark database (a name ending in `_test`) from the snapshot and the profiles. |
| `pool.py`, `pool.jsonl` | Builds the set of pairs to judge (the union of the top 10 of M1 to M4 per profile), as references. |
| `cards.py` | Prints a profile and the compact cards of its pooled jobs, in an order that hides the method. |
| `labels.jsonl` | One graded label per pooled pair. |
| `metrics.py` | The metrics and the paired bootstrap, pure functions. |
| `methods.py` | The six methods over one profile. |
| `run.py`, `report.py`, `results.json`, `REPORT.md` | The runner, the report writer, and the recorded run. |

## The six methods

All six run on the same snapshot, for the same profile, through the same constraint filter, so the comparison isolates
ranking. The filter is the one the product uses: it sits inside `match.api.retrieve` (DATA_MODEL 6.1), which compiles the
profile's hard constraints in the database and ranks only the jobs that break none of them.

| Method | What it is | Model calls |
|---|---|---|
| M1 Keyword | `match.api.retrieve(mode="keyword", limit=300, depth=300)`: Postgres full-text rank over the profile's keywords | none |
| M2 Vector | `retrieve(mode="vector", limit=300, depth=300)`: bge-small cosine distance to the profile passage | none |
| M3 Hybrid | `retrieve(mode="hybrid", limit=300, depth=300)`: M1 and M2 fused by reciprocal rank (k = 60) | none |
| M4 Hybrid plus rules | M3's first 50, re-ordered by the match run's own pre-rank (`app.match.service._prerank`, which applies `ranking.prerank_score`: fusion score as a share of the best, soft-preference match, seniority distance, recency bucket); ties to the lower job id | none |
| M5 Full cascade | M4's first 40 that have extracted requirements, each scored by `evidence.build_request`, `evidence.score` and `validator.validate` (one retry), then ordered by `ranking.final_score` (the verdict fit plus the same soft signals) | `match.evidence.score` on up to 40 pairs |
| M6 Score only what is shown | M4's first 5, in M4's order, scored the same way for their requirement tables only | up to 5 pairs |

M4 calls the match run's private `_prerank` on purpose: the benchmark measures the code the nightly run executes, not a
copy of it, and a rename there stops this script with an import error instead of silently measuring something else.
Recency is measured at the snapshot's export time, so a re-run on another day gives the same lists.

M6's list is M4's list, so its `ndcg_at_5` equals M4's by construction. M6 exists to price the requirement tables
(AI_SYSTEM 12.5): the question M5 versus M6 answers is whether scoring 40 pairs instead of 5 buys better ordering.

## Metrics (pre-registered)

Per profile first, then the mean over profiles. A **grade** is the label of a (profile, job) pair, 0 to 3
(`GUIDELINE.md`). A job that is not in `labels.jsonl` for that profile is **unjudged** and counts as grade 0.
**Relevant** means grade 2 or 3. The **ideal** list of a profile is its labelled jobs sorted by grade, best first.

| Metric | Definition |
|---|---|
| `ndcg_at_5` (headline) | DCG of the first five jobs of the method's list, gain 2^grade - 1, discount 1 / log2(position + 1), divided by the DCG of the ideal first five. A profile whose ideal DCG is 0 (nothing labelled 1 or more) is left out of the mean and counted. |
| `precision_at_5` (headline) | Relevant jobs among the first five, divided by 5. A list shorter than five is still divided by 5. |
| `recall_at_300`, `recall_at_40` | Relevant jobs found in the first 300 (40) of the list, divided by the profile's relevant labelled jobs. A profile with no relevant labelled job is left out of the mean and counted. Recall is relative to the pool, so it is an upper bound on recall over the whole snapshot. M4's list has 50 jobs, M5's 40 and M6's 5, so for them `recall_at_300` is recall over the whole list. |
| `unjudged_at_5_rate` | Positions among the first five that are not in `labels.jsonl`, over all profiles. |
| `constraint_violations` | Jobs anywhere in the list for which `match.checker.violations(job, constraints)` is not empty, summed over profiles. The checker is the independent Python implementation, not the SQL the filter ran. |
| `cost_per_candidate_day_usd` | The summed `cost_microusd` of the method's `llm_calls` rows for one profile, in dollars: exactly 0 for M1 to M4, which call no model. Beside it, local CPU seconds (`time.process_time`) around the method, with the profile embedding timed on its own (AI_SYSTEM 12.5). |
| latency p50, p95 | Wall-clock seconds (`time.perf_counter`) for the method on one profile, the profile embedding included for M2 to M4 (each method embeds the profile itself, as the product does), median and 95th percentile over the profiles. |

Slices: profiles with at least one hard constraint, and profiles with none.

**Paired bootstrap.** For each metric and each method other than M3, the per-profile difference from M3 is resampled
over profiles with replacement, 2,000 times, with `random.Random(20261123)`; the 95% interval is the 2.5th and 97.5th
percentile of the resampled mean differences (nearest rank). Each method's own mean carries an interval from the same
resamples. M5 against M6 on `ndcg_at_5` is computed the same way, when both have run. A difference is *detected* only
when its interval excludes 0.

## Pre-registered bars and rules

Written on 2026-11-23 (AI_SYSTEM 12.1 rule 1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). No run of this benchmark existed when this was written: this file and `GUIDELINE.md` are the first
files of the folder in git history, committed before the snapshot, the profiles, the loader, the pool, the labels or any
run. Since then only the parts that describe the finished data (the "Status of the data" section) were added.

**Bars of a run** (all four must hold; otherwise the run's `score` is 0.0):

1. `constraint_violations` is 0 for every method that ran, on every profile. A job that breaks a hard constraint in any
   position of any list fails the run (MAT-01, invariant 1).
2. M1 to M4 each produce a list for every one of the 20 profiles (a list may be empty only when the retrieval had
   nothing to rank on, and the report names each such profile).
3. `unjudged_at_5_rate` is 0 for M1 to M4: the pool holds the top 10 of each, so their first five are always judged. A
   higher figure means the pool or the loader is broken.
4. Every pooled pair has a label, and every label carries the agent-drafted marker.

`score` is 1.0 when all four hold and 0.0 otherwise, as the other multi-bar suites record it. There is no floor in
`evals/thresholds.toml`: this is a report with no gate (the labels are agent-drafted and two methods are not run), so
the CI gate does not read it.

**The M5 versus M6 rule** (AI_SYSTEM 12.5, fixed before any live run): M5 replaces M6 as the default only if the lower
bound of its `ndcg_at_5` difference against M6 is above zero; otherwise M6 ships and scoring volume falls. This is
decided from a live run with 20 profiles at least, and is not decided here.

**Expectations written before the run (not bars).** (E1) M3 should not be worse than M1 or M2 on `ndcg_at_5`, since it
combines them. (E2) M4 should not be worse than M3, since it adds the preference and level signals. The run reports each
as supported (the interval excludes 0 in the expected direction), contradicted (it excludes 0 the other way) or
undetermined (it contains 0). With 20 profiles undetermined is the likely answer, and it is reported as such.

## How the pairs to judge are chosen (pooling)

For every profile the pool is the union of the first 10 jobs of M1, M2, M3 and M4. The labeller sees the jobs in an order
that hides which method returned them. Pooling judges what the four cheap methods return and nothing deeper, so a method
that ranks well but returns different jobs (M5 after a live run) can show unjudged positions; `unjudged_at_5_rate` says
how many, and an unjudged job counts as not relevant, which can only understate it.

## Known limits

- **One agent wrote the profiles and judged the pairs.** The labeller knows what each profile is meant to want, which
  removes ambiguity a real reviewer would face and may favour methods that match the profile's own words.
- **Pool bias.** See above: methods outside the pool are disadvantaged.
- **The postings carry only what the index stores.** Ingest stores no pay and no seniority, so a salary-floor or
  seniority constraint filters nothing here (an unstated field passes the filter and is flagged unverified), and the
  seniority distance in M4 is 0. The constraint-safety property suite, not this benchmark, tests those fields.
- **Posting vectors are made from the title and the start of the description**, because the snapshot has no extracted
  requirements (extraction needs a model key); this is what the index holds in the same situation.
- **Latency and CPU seconds are from one machine**, with the posting vectors already stored; they compare methods on this
  machine and are not capacity figures.

## Status of the data

Added after the snapshot, the profiles and the labels were made. The sections above were not changed, except that the
files table now lists `target.py` (the guard that refuses any database whose name does not end in `_test`), which was
written after the table.

**Snapshot** (`snapshot.jsonl`, 4,995,603 bytes; `snapshot.meta.json`, 1.8 KB). Exported on 2026-11-23 from a restored
copy of a `pg_dump` of the live index `app` (Alembic `0002_platform`, 11,357 jobs), upgraded to head, because `app` is
below the Phase 2/3 merge and the export refuses it. `app` itself was only dumped; the copy and the dump were deleted
afterwards. Of 9,996 open, listed, canonical postings, 2,038 were dropped because their text held an email address or a
phone number, which left 7,958. The target was 1,000 postings and the count fell to 758 to keep the file under 5 MB.
They come from 57 sources (Greenhouse 512, Ashby 211, Lever 35) in 19 countries; 101 of them had no country in the
index.

**What the store path changes.** `load.py` puts the postings in through `ingest.fetch.store`, which reads each place and
arrangement again with the current parser: 87 of the 101 postings that had no country in the live index get one, 11
get a different one, and no arrangement changes (14 postings are left with no country). The benchmark therefore ranks the
current parser's reading of each posting, not the values the live index stored.

**What the index does not hold.** No posting states pay or a level, so a salary-floor or level constraint filters
nothing (an unstated field passes and is flagged unverified) and M4's level distance is always 0. Every posting was first
seen on the same day, so the recency bucket is the same for all of them and cannot reorder anything: here M4 tests the
soft preferences (target title, arrangement) only.

**Profiles.** Twenty invented ones, `p01` to `p20`, written from the title mix of the snapshot (software, infrastructure,
security and AI engineering, engineering management, solutions and sales engineering, enterprise sales, sales
development, customer success, partnerships, product management, finance, legal, recruiting, marketing and freight
operations). Twelve carry at least one hard constraint (location 8, remote policy 2, work authorization 1, employment type
1, level 1, salary floor 1, excluded company 2) and eight carry none; the salary floor of `p10` and the level
constraint of `p06` filter nothing, as above. Before the pool existed, the remote-policy constraint of `p13` was changed
from "remote or hybrid" to "remote only": the postings state "remote" or "on-site or hybrid", so "remote or hybrid"
accepted every posting.

**Pool and labels.** 366 pairs (273 different postings) for the 20 profiles, 12 to 24 per profile; the plan expected about
500, and the four methods overlap more than that. Grades: 0 for 114 pairs, 1 for 86, 2 for 90, 3 for 76; 166 pairs (45%)
are relevant, and every profile has at least 4. One agent graded them in a single pass in the hidden order (the decisions
made are at the end of `GUIDELINE.md`). A card shows the first 600 characters of a posting, which are mostly company
boilerplate, so most grades rest on the title, the place and the arrangement.

**Database name.** Load the benchmark into a database of its own (for example `app_bench_test`), not the test suite's
database: the suite empties the tables it shares with the benchmark.

**A dry run came before the recorded run.** After the labels were written, the runner was run once without recording, to
see that it worked end to end, and its numbers were seen. Nothing that decides M1 to M4's lists or the quality metrics was
changed afterwards (the recorded run reproduces the dry run's quality numbers exactly); three things were:

- *CPU seconds.* The dry run showed CPU seconds about ten times the wall-clock seconds for every method, M1 included. A
  probe showed why: the embedding runtime's worker threads keep spinning for about half a second after each call (3.9 CPU
  seconds in 0.5 s of idleness), and that spin was counted against whichever method ran next. Each timed call now starts
  after a 0.6 s pause. CPU seconds are the whole process's during the call, the embedding's threads included.
- *M5 and M6 without requirements.* The first version "ran" them with an empty list when no job had extracted
  requirements, which would have reported an ndcg of 0 for a method that was never run. They now check that the gateway
  has a route before asking anything (`not run: needs a model key`) and report `not run` again when no job has
  requirements (extraction is a model-backed step that a benchmark database has not had).
- *Report wording only* (the names of the methods that did not run and a note on unadjusted intervals).

## Commands

All of these refuse any database whose name does not end in `_test`. Run them from `backend/`, with
`DATABASE_URL_MIGRATE`, `DATABASE_URL_API`, `DATABASE_URL_WORKER` and `DATABASE_URL_MATCH` all pointing at the same
scratch database, so that nothing can fall back to the default, which is the live one.

```
uv run python ../evals/matching_benchmark/load.py --database-url postgresql://app:...@127.0.0.1:5433/app_bench_test
uv run python ../evals/matching_benchmark/pool.py --database-url ...      # rebuilds pool.jsonl from the snapshot
uv run python ../evals/matching_benchmark/cards.py p01 --database-url ... # the cards of one profile, for labelling
uv run python ../evals/matching_benchmark/run.py --database-url ...       # M1 to M4; M5 and M6 stop without a key
uv run python ../evals/matching_benchmark/report.py                       # REPORT.md from results.json
```

## Manual checks (they need the maintainer or a model key)

1. The maintainer reviews the labels; a second rater labels a random 20% of the pairs without seeing the first labels;
   Cohen's kappa (quadratic-weighted, the grades are ordered) is reported; disagreements amend `GUIDELINE.md`. Grow the
   set toward 50 profiles and 1,000 to 2,000 pairs.
2. With a model key: extract the requirements of the jobs M5 will score, run M5 and M6 live, report `evidence_faithful_rate`
   for them, and apply the M5-versus-M6 rule above. Publish no quality number for M5 or M6 before this.
3. Re-run M1 to M4 after the labels change and replace `results.json` and `REPORT.md`.
