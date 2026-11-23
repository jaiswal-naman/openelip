# Matching benchmark report

**Labels: agent-drafted, maintainer review pending.** One agent wrote the 20 invented profiles and graded every pooled pair; nobody has reviewed the labels and no second rater has graded a sample. The profiles and labels are small (the target is 50 profiles and 1,000 to 2,000 pairs), so every interval below is wide. These numbers describe this snapshot, these profiles and these labels, on one machine, and nothing else (AI_SYSTEM 12.1, invariant 8). Read `README.md` for the pre-registered metrics and bars, and its limits.

Recorded run: 2026-11-23T12:34:26+05:30; eval_runs row `fe21b577-ae7a-4497-9257-188047c2a6b9` in the scratch database `app_p4_07_bench_test` (dropped after the run, so the row is not kept: the numbers here and in `results.json` are the record). 758 public postings exported 2026-11-23T06:26:04+00:00; 20 invented profiles; 366 pooled pairs graded 0 to 3 (grade 0: 114, 1: 86, 2: 90, 3: 76). Ranker `rules-2026-11-a`, embedding model `BAAI/bge-small-en-v1.5`.

## Headline

| Method | ndcg_at_5 (95% interval) | precision_at_5 | recall_at_40 | recall_at_300 | constraint_violations | unjudged at 5 | cost per candidate-day | latency p50 / p95 (s) | CPU s (embedding CPU s) |
|---|---|---|---|---|---|---|---|---|---|
| M1 Keyword | 0.598 (0.501 to 0.689) | 0.600 | 0.931 | 1.000 | 0 | 0.0% | $0.000000 | 0.035 / 0.071 | 0.002 (0.000) |
| M2 Vector | 0.684 (0.606 to 0.752) | 0.680 | 0.855 | 0.987 | 0 | 0.0% | $0.000000 | 0.032 / 0.039 | 0.268 (0.155) |
| M3 Hybrid | 0.777 (0.710 to 0.837) | 0.780 | 0.987 | 1.000 | 0 | 0.0% | $0.000000 | 0.053 / 0.081 | 0.438 (0.141) |
| M4 Hybrid plus rules | 0.795 (0.733 to 0.850) | 0.810 | 0.987 | 0.994 | 0 | 0.0% | $0.000000 | 0.055 / 0.107 | 0.491 (0.120) |
| M5 Full cascade | not run: needs a model key | | | | | | | | |
| M6 Score only what is shown | not run: needs a model key | | | | | | | | |

ndcg_at_5, precision_at_5 and the recalls are means over profiles; recall is relative to the pooled, labelled jobs. Cost is the model dollars per candidate-day: exactly zero for the methods that call no model. CPU seconds are the process CPU seconds of one call, after an idle pause, with the part spent in the local embedding model in brackets (see the limits).

## Differences from M3 (paired bootstrap)

Profiles are resampled with replacement 2,000 times (seed 20261123); the interval is the 2.5th to 97.5th percentile of the resampled mean differences. A difference counts as detected only when its interval excludes 0.

| Method | Metric | Mean difference from M3 | 95% interval | Reading |
|---|---|---|---|---|
| M1 | ndcg_at_5 | -0.179 | -0.256 to -0.109 | detected: lower than M3 |
| M1 | precision_at_5 | -0.180 | -0.260 to -0.090 | detected: lower than M3 |
| M1 | recall_at_40 | -0.056 | -0.113 to -0.009 | detected: lower than M3 |
| M1 | recall_at_300 | +0.000 | 0.000 to 0.000 | no detectable difference |
| M2 | ndcg_at_5 | -0.093 | -0.159 to -0.030 | detected: lower than M3 |
| M2 | precision_at_5 | -0.100 | -0.170 to -0.030 | detected: lower than M3 |
| M2 | recall_at_40 | -0.132 | -0.196 to -0.076 | detected: lower than M3 |
| M2 | recall_at_300 | -0.013 | -0.034 to 0.000 | no detectable difference |
| M4 | ndcg_at_5 | +0.018 | -0.007 to 0.045 | no detectable difference |
| M4 | precision_at_5 | +0.030 | -0.010 to 0.070 | no detectable difference |
| M4 | recall_at_40 | +0.000 | 0.000 to 0.000 | no detectable difference |
| M4 | recall_at_300 | -0.006 | -0.019 to 0.000 | no detectable difference |

## Slices

Profiles with at least one hard constraint, and profiles with none. These are secondary comparisons on 12 and 8 profiles, and none of the intervals in this report is adjusted for how many were computed: one interval that excludes 0 among several is weak evidence.

| Method | Slice | Profiles | ndcg_at_5 | precision_at_5 | ndcg_at_5 difference from M3 (95% interval) |
|---|---|---|---|---|---|
| M1 | with constraints | 12 | 0.616 | 0.583 | -0.190 (-0.286 to -0.106) |
| M1 | without constraints | 8 | 0.570 | 0.625 | -0.161 (-0.288 to -0.051) |
| M2 | with constraints | 12 | 0.700 | 0.667 | -0.107 (-0.189 to -0.033) |
| M2 | without constraints | 8 | 0.660 | 0.700 | -0.072 (-0.178 to 0.033) |
| M3 | with constraints | 12 | 0.806 | 0.767 | (baseline) |
| M3 | without constraints | 8 | 0.732 | 0.800 | (baseline) |
| M4 | with constraints | 12 | 0.841 | 0.833 | +0.034 (0.007 to 0.067) |
| M4 | without constraints | 8 | 0.726 | 0.775 | -0.005 (-0.046 to 0.036) |

## Pre-registered bars

- 1. constraint_violations is 0 for every method that ran: holds
- 2. M1 to M4 each produce a list for every profile: holds
- 3. unjudged_at_5 is 0 for M1 to M4: holds
- 4. every pooled pair has a label, each marked agent-drafted: holds
- the run's score is 1.0. There is no floor in `evals/thresholds.toml`: this is a report, not a gate.

## Expectations written before the run (not bars)

- E1: M3 not worse than M1 on ndcg_at_5: supported
- E1: M3 not worse than M2 on ndcg_at_5: supported
- E2: M4 not worse than M3 on ndcg_at_5: undetermined

## M5 and M6

- M5: not run: needs a model key.
- M6: not run: needs a model key.
- The pre-registered decision between M5 and M6 (M5 replaces M6 only if the lower bound of its ndcg_at_5 difference against M6 is above zero) waits for a live run: it needs a model key, and the postings' requirements extracted first.

## Per profile

| Profile | Hard constraints | M1 ndcg_at_5 | M2 ndcg_at_5 | M3 ndcg_at_5 | M4 ndcg_at_5 |
|---|---|---|---|---|---|
| p01 Backend engineer, eight years, Python and Go | no | 0.252 | 0.381 | 0.636 | 0.650 |
| p02 Site reliability engineer, eight years, Kubernetes and Terraform | yes | 0.677 | 0.723 | 0.764 | 0.841 |
| p03 Machine learning engineer, six years, PyTorch and language models | no | 0.645 | 0.358 | 0.627 | 0.680 |
| p04 Full-stack engineer, seven years, React and TypeScript | yes | 0.269 | 0.804 | 0.869 | 0.925 |
| p05 Security engineer, eight years, application and cloud security | no | 0.854 | 0.869 | 0.842 | 0.925 |
| p06 Engineering manager, eleven years, distributed systems | yes | 0.351 | 0.695 | 0.633 | 0.703 |
| p07 Solutions engineer, seven years, integrations and customer workshops | yes | 0.358 | 0.453 | 0.564 | 0.733 |
| p08 Computer science student graduating in 2027, looking for internships and new-graduate roles | yes | 0.732 | 0.818 | 0.926 | 0.933 |
| p09 Data scientist, six years, experimentation and forecasting | no | 0.326 | 0.498 | 0.490 | 0.510 |
| p10 Solutions architect, twelve years, cloud and data platforms | yes | 0.651 | 0.760 | 0.910 | 0.895 |
| p11 Enterprise account executive, twelve years, software and security sales | yes | 0.614 | 0.537 | 0.731 | 0.731 |
| p12 Sales development representative, two years, outbound prospecting | no | 0.817 | 0.723 | 0.903 | 0.878 |
| p13 Customer success manager, eight years, onboarding and renewals | yes | 0.803 | 0.488 | 0.944 | 0.950 |
| p14 Partner manager, nine years, channel and alliances | yes | 0.977 | 0.970 | 0.995 | 0.959 |
| p15 Senior product manager, ten years, business-to-business payments | no | 0.387 | 0.732 | 0.878 | 0.765 |
| p16 Finance manager, ten years, month-end close, audit and budgeting | yes | 0.481 | 0.744 | 0.823 | 0.844 |
| p17 Commercial counsel, ten years, contracts, privacy and compliance | yes | 0.531 | 0.687 | 0.571 | 0.571 |
| p18 Technical recruiter, six years, engineering and data hiring | no | 0.526 | 0.813 | 0.673 | 0.673 |
| p19 Field marketing manager, eight years, events and account-based marketing | yes | 0.948 | 0.720 | 0.948 | 1.000 |
| p20 Freight operations associate, five years, ocean and air freight and customs | no | 0.757 | 0.903 | 0.806 | 0.731 |

## Limits

- The labels are agent-drafted and unreviewed, by the agent that wrote the profiles.
- The pool is the top 10 of M1 to M4, so a method outside the pool (M5 after a live run) can show unjudged positions, which count as not relevant.
- Ingest stores no pay and no level, so salary-floor and level constraints filter nothing here, M4's level distance is 0, and every posting was first seen on the same day, so M4's recency term cannot reorder anything.
- A card showed the first 600 characters of a posting, mostly company boilerplate, so most grades rest on the title.
- Latency and CPU are from one machine with the posting vectors already stored. The embedding runtime's worker threads spin for about half a second after each call, so each timed call starts after a 0.6 s pause; CPU seconds are the whole process's during the call, the embedding's threads included.

## Re-running

```
cd backend   # all four DATABASE_URL_* variables pointing at one scratch database ending in _test
uv run python ../evals/matching_benchmark/load.py --database-url <that database>
uv run python ../evals/matching_benchmark/run.py --database-url <that database>
uv run python ../evals/matching_benchmark/report.py
```
