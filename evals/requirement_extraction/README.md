# Requirement-extraction gold set

50 real job postings, each with the qualifications a person reads out of it: the exact words in the posting, where
they sit (a character span), whether they are required or preferred, their kind, and the years of experience they ask
for. It grades requirement extraction (IDX-08) and is the set the heading-rules baseline and the model are scored on.

**Status: labels drafted by an agent, review pending.** Every label says `labeller: "agent"`. The maintainer reviews at
least 20% of the set (10 of the 50 postings) before any score from it is published, in the way AI_SYSTEM 12.2 describes
for a second rater: a person labels those postings from `GUIDELINE.md` without looking at these labels, agreement is
reported, and disagreements amend the guideline. Until then a report must say "labels drafted by an agent, review
pending". The 10 judgement calls under "Decisions made while labelling" in the guideline are the place to start.

## Files

| File | What it is |
|---|---|
| `GUIDELINE.md` | What counts as a requirement, how to quote it, importance, kind and years, worked example, decisions made while labelling. |
| `postings.jsonl` | The 50 postings: `id, split, ats, board, external_id, title, description`. |
| `labels.jsonl` | The hand-written labels: `posting_id, requirements: [{quote, importance, kind, min_years, occurrence?}], labeller`. |
| `gold.jsonl` | The result scorers read: `posting_id, split, title, description, requirements: [{start, end, text, importance, kind, min_years}]`. Written by `resolve.py`, never by hand. |
| `build_sample.py` | Draws the postings from the index (read-only) and writes `postings.jsonl`. |
| `resolve.py` | Finds each quote in its posting and writes the spans; `--check` proves `gold.jsonl` is current. |

Spans are Python string indices, that is Unicode code points: the same unit PostgreSQL `substr` uses. Anything that
slices in UTF-16 (JavaScript `string.slice`) is wrong on postings with emoji; use `Array.from`. None of the 507 spans
is affected by an emoji ahead of it, but req-034 contains emoji, so the hazard is real.

## Where it came from

- **Source:** the index's own copy of postings fetched from the public Greenhouse, Lever and Ashby board endpoints.
  `build_sample.py` read it from database `app` with a read-only session (SELECT only). All 50 postings were first seen
  and last verified in the index on 2026-11-22, and their text still matched the index byte for byte when this README
  was written (2026-11-23). The text is third-party text that companies published on their own job boards; it is kept
  here only to grade extraction.
- **Universe:** 9,996 open postings that are not duplicates of another. 532 were left out for being longer than 10,000
  characters and 1,653 more for containing an email address or a phone-number pattern, so the pool is 7,811 (Ashby 2,185,
  Greenhouse 5,266, Lever 360). Excluding postings with an email address is a selection effect: a company that writes
  an address into every posting is absent from the set.
- **Draw:** `random.Random(20261122)` over the pool sorted by `(ats, board, external_id)`: 10 from each ATS first, then
  20 more from what was left, then shuffled. The first 10 drawn are `dev` (req-001 to req-010: 4 Ashby, 3 Greenhouse, 3
  Lever) and the other 40 are `test`. The result is 16 Ashby, 23 Greenhouse and 11 Lever postings from 27 boards.
- **Not reproducible later:** the index changes every day, so running the builder again gives different postings. The
  committed files are the record, and the builder refuses to overwrite `postings.jsonl`.

## How it was labelled

An agent read each posting in full and labelled it from `GUIDELINE.md`: quotes were copied character for character from
the text, then `resolve.py` located each one and reported anything missing, ambiguous or overlapping. No extractor, no
heading rule and no model was run or read on these postings. The guideline's rules and worked example were written
before the first posting was read; ten cases they did not settle were decided while labelling and are listed at the end
of the guideline.

## What the labels cover

| | Count |
|---|---|
| Items | 507: 409 required, 98 preferred; 34 of the 50 postings have at least one preferred item |
| Kinds | skill 279, experience 172, location 23, education 17, other 11, certification 5, authorization 0 |
| Items that state a number of years | 42 (`min_years` set) |
| Items per posting | 3 to 30, median 9; the items cover 19.5% of the posting text |
| Dev and test | dev 10 postings, 112 items; test 40 postings, 395 items |

## Limits to keep in mind

- **Fifty is a start, not the target.** AI_SYSTEM 4.3 and 12.2 aim at 300 postings with a second rater on 20%. Hand
  labelling is the limit, so 50 ship now. A score on 50 postings carries a wide interval and is reported with its
  counts and a Wilson interval (AI_SYSTEM 12.1), never as a bare percentage. Growing the set is a manual follow-up.
- **Some kinds are barely there.** No posting states a work-authorization requirement (the one visa line, in req-040,
  says what the employer offers), and only 5 items are certifications. Per-kind scores for those kinds mean nothing yet.
- **Two pairs are near-duplicates.** req-001 and req-049 are 99.9% the same text, and req-032 and req-043 are 97.6% the
  same; the index kept both because their titles differ by a space or a word. req-001 is a `dev` posting and req-049 a
  `test` posting, so req-049 is not unseen test data: a score that wants a clean test split should leave it out (5 of
  the 395 test items). Counting each pair once leaves 48 distinct postings.
- **One board dominates the Lever slice.** Paytm supplies 8 of the 50 postings, which is 8 of the 11 from Lever, and
  several of them share a template.
- **One labeller, English only.** The agent's reading is the only reading so far, and every posting is in English.
- **A cap.** req-037 has 30 items, the per-job cap in AI_SYSTEM 4.3.
- **The index text is kept as it is.** req-033 contains a literal `&amp;` and req-049 a literal `&#39;` that the index
  never decoded. The gold set holds the index's text exactly so spans match what the index stores.
- **Personal data.** Postings with an email address or phone number are excluded, and no label uses a person's name.
  req-009's text credits a blog post to one named company officer; it is public text, not a contact person, and it is
  left as the index holds it.

## The test split is frozen

The 10 `dev` postings are for writing prompts and rules. The 40 `test` postings are for one recorded run. Tuning a
prompt, a rule or a threshold against the test split is not allowed (AI_SYSTEM 12.1, rule 3), and a failing test posting
is reported, not patched around. If review finds a wrong label, correct `labels.jsonl`, run `resolve.py`, commit both
files and say so in the commit message; never change code to fit a label.

## Commands

```
uv run --project backend python evals/requirement_extraction/resolve.py          # write gold.jsonl from the labels
uv run --project backend python evals/requirement_extraction/resolve.py --check  # exit 0 only if gold.jsonl is current
```

`--check` also validates the postings (50, split 10 and 40, at least 10 per ATS, no email or phone number, none over
10,000 characters), that every posting has a label row, and every label's quote, importance, kind and years. Building
needs a read-only `DATABASE_URL` and refuses to run while `postings.jsonl` exists.

## Pre-registered bars

Written on 2026-11-23 (AI_SYSTEM 12.1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). One development run of the rules baseline happened three minutes before these bars were committed, so
the order cannot be shown from git; the bar values were not tuned to it (the rules baseline misses `req_item_f1 >= 0.85`).
No model run exists yet: the first live run is the real test of these bars.

**What is scored.** One run scores one split, with the items pooled over its postings. The test split is the 40 test
postings without `req-049`: that posting is a near-copy of dev posting `req-001` (99.9% the same text), so it is not
unseen data and is left out of every test score (it stays in `gold.jsonl` and in the recordings). A test score covers
39 postings and 390 items. `score.py` reads each posting's text from `gold.jsonl`, never from the live index, which holds
a literal `&amp;` that the gold text keeps (see "Limits to keep in mind").

- `req_item_f1`: predicted items and gold items are paired one to one, the pair with the greatest overlap first, where
  overlap is intersection over union of the two character spans (in code points); a pair is a match at 0.5 or more.
  Precision is matches over predicted items, recall is matches over gold items, and `req_item_f1` is their harmonic
  mean. Precision and recall are reported with 95% Wilson intervals (AI_SYSTEM 12.1).
- `req_importance_accuracy`: of the matched pairs, the share whose `required` or `preferred` agrees, with its Wilson
  interval.
- `req_spans_resolved`: the share of predicted rows whose text is exactly the posting's text at their span.

**Bars.**

1. Every run: `req_spans_resolved = 1.0`. Absolute: AI_SYSTEM 12.8 makes a stored citation that does not resolve a
   blocker, whatever the other numbers are.
2. A live-model run on the test split: `req_item_f1 >= 0.85` and `req_importance_accuracy >= 0.90` (the targets of
   AI_SYSTEM 4.3). No live run exists, because there is no model key. `score.py` refuses a recording that is not a
   fixture, so it never judges a live run by accident; applying these two bars in code is part of recording the first
   live run (a manual check).
3. No bar for the rules method (`--method rules`) or for a replay of `rules_fixture` recordings: they are baselines, and
   a replay shows that the code path works. The CI gate holds the replay to the stored baseline instead: its
   `req_item_f1` and `req_importance_accuracy` may not fall below the replay stored in `baseline.json`. A replay is
   exact, so that margin is 0; the two-point margin of AI_SYSTEM 12.8 is for runs that vary.

The CI gate reads one number, `metrics.score` (floor 1.0 in `evals/thresholds.toml`). `score` is 1.0 only when every bar
that applies to the run holds and 0.0 otherwise; the honest numbers are the other metrics, always reported with the
words "labels drafted by an agent, review pending". The maintainer's review of at least 10 of the 50 postings comes
before any figure from this set is published.

## How the suite runs

| File | What it is |
|---|---|
| `score.py` | The `requirement_extraction` suite. `--method model` (the default) replays `recordings.jsonl` through the real task path; `--method rules` scores the heading rules on the posting text; `--split dev\|test` (default test); `--record` writes the run to the eval tables, and `--report FILE` stores it in a baseline file. |
| `record_fixture.py` | Builds `recordings.jsonl`: for every gold posting the answer the heading rules give, in the fake provider's format. |
| `recordings.jsonl` | One line per gold posting: `posting_id, split, source, request_sha256, answers`. `source` is `rules_fixture`, which says these are not model output. |
| `baseline.json` | The two recorded runs on the test split, as the database holds them: `runs.replay` and `runs.rules`. |

A replay goes through the code a live extraction goes through: the gateway's fake provider hands the recorded answer to
`job.requirements.extract`, the validator computes every span, and the rows are scored. `request_sha256` is a hash of the
exact messages the task sends for that posting, so a replay stops, and says to run `record_fixture.py`, when the prompt or
the segmenter has changed since the recording was made. A replay needs `DATABASE_URL_WORKER` pointing at a migrated
database (the gateway writes its ledger there); the rules method needs none unless `--record` is given. A recorded run
cannot be deleted, so `--record` and a replay refuse unless that database's name ends in `_test` or is exactly `evals`
(the CI eval job's database); `--live` writes to any other one, and is for use on purpose only.

The suite is scored by code (span overlap), not by exact match, so it is not in the `SUITES` table of
`backend/app/evals/runner.py`: that table replays answers and compares them whole. `score.py --record` writes the same
tables, and `python -m app.evals gate requirement_extraction --run ID` judges the run from them.

In CI the `eval-gate` job (`.github/workflows/ci.yml`) runs `score.py --record` for this suite and then
`python -m app.evals gate requirement_extraction --run ID`, as it does for every suite. The floor in
`evals/thresholds.toml` is 1.0 and `score` is 1.0 only when every bar that applies holds: every citation resolves, and
`req_item_f1` and `req_importance_accuracy` are not below the replay stored in `baseline.json`. A change to the prompt,
the segmenter, the validator or the heading rules therefore has to rebuild the recordings (`record_fixture.py`) and, if
the numbers move, store new runs (`--record --report baseline.json`, once for each method) with the reason in the commit
message.

A live recording needs a model key and is a manual check: `score.py` refuses any recording that is not a `rules_fixture`
one, and applying the live bars of the section above in code belongs with the first live run.

**Until a live run passes, no job is extracted, key or no key.** A route may serve a task only when a passed gold-set run
for that task and model exists (AI_SYSTEM 2.2). The extraction sweep checks this before it queues anything: it needs a
passed `requirement_extraction` run in `eval_runs` on the test split, by the model method, whose config names the
route's `model` and the current `prompt_version`. Otherwise it queues nothing and logs `unproven`. The replay CI records
names no model, so it never counts; the first live run has to record `model` in its config for the sweep to start, and a
changed prompt (`prompt_version`) needs a new live run.

## Recorded runs on the test split (2026-11-23)

**Labels drafted by an agent, review pending.** Two runs of `score.py --record --report baseline.json` on the 39 clean test
postings, at commit `6efe3af`, in a scratch database whose name ends in `_test` (not the live one). The rules run is
`641a3723-6e6c-45b0-8c31-a008fcff7e36` and the replay `34ece495-f9a9-4556-ae59-a3eb5ff69623`. The two give the same
numbers, because the replay's answers are the rules' rows.

| | |
|---|---|
| Items | 390 in the gold set, 445 predicted, 246 matched at IoU 0.5 or more |
| Precision | 0.5528 (95% Wilson interval 0.5064 to 0.5984) |
| Recall | 0.6308 (0.5818 to 0.6772) |
| `req_item_f1` | 0.5892 |
| `req_importance_accuracy` | 0.8780 (0.8312 to 0.9132), over the 246 matched items |
| `req_spans_resolved` | 1.0 (every one of the 445 rows is the posting's own text at its span) |
| Turned away by the validator | nothing |
| Bars | `req_spans_resolved = 1.0` met. No other bar applies to these two runs. |

What these numbers do and do not show:

- **The rules run is a baseline, not a model quality number.** It is a real measurement of the heading rules, which were
  written before the gold set was read and were not changed after this run. `req_item_f1` is 0.26 below the 0.85 that a
  live model must reach; AI_SYSTEM 4.3's rule that rules within three points of the model replace it can only be
  applied once a live run exists.
- **The replay is not a model quality number either.** Its answers are the rules' rows written as a model would write
  them, so it shows that the path from a recorded answer to scored rows works and is exact, and it is what CI replays.
- The labels were drafted by one agent and are not yet reviewed, so these figures are provisional until 10 postings have
  been checked. If review corrects a label, correct `labels.jsonl`, run `resolve.py` and `record_fixture.py`, run the two
  commands again, and say so in the commit message; never change code to fit a label.
- 39 postings from three ATS products on one day: the intervals describe this sample, not job postings in general.
  `req-032` and `req-043` are near-identical (97.6%) and both count.
