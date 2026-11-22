# Location gold sample

400 real job postings' location inputs, each labelled with the country and city they state, or null when they state
none. It grades the location parser (IDX-06: country and city correct at least 95% of the time on this sample, and a
place nobody stated stays unknown).

**Status: labels drafted by an agent, review pending.** Every row says `labeller: "agent"`. The maintainer reviews at
least 20% of the test split (60 of the 300 rows) before any accuracy figure from this sample is published. Until then
a report must say "labels drafted by an agent, review pending". The review is also where the 10 judgement calls in
`GUIDELINE.md` ("Decisions made while labelling") should be read first.

## Files

| File | What it is |
|---|---|
| `GUIDELINE.md` | The labelling rules with worked examples, and the decisions made while labelling. |
| `sample.jsonl` | 400 rows: `id, split, ats, board, external_id, location_raw, country_hint, remote_hint, label_country, label_city, labeller`. |
| `build_sample.py` | Draws the sample from the live boards (build) or validates the committed file (`--check`). |
| `score.py` | The `location_accuracy` suite: runs `parse_location` over a split, scores it, and records the run. |
| `baseline.json` | The one recorded run on the test split, written by `score.py --record --report`. |

`location_raw`, `country_hint` and `remote_hint` are what the board published. `label_country` is an ISO 3166-1 alpha-2
code or null. `label_city` is a city name or null. Null means the inputs do not say.

## Where it came from

- **Source:** the three public job-board endpoints the index itself reads (Greenhouse, Lever, Ashby), fetched on
  2026-11-23 for the 71 boards that were active in the index that day (43 Greenhouse, 19 Ashby, 9 Lever). No board
  failed. Only the location fields and ids are kept; no posting text is stored in this file.
- **Exclusions:** a posting whose description, title or location contains an email address or a phone-number pattern is
  left out before the draw: 2,240 of 11,360 fetched postings (19.7%). Almost all are email addresses (generic careers,
  accommodations or privacy contacts); the phone pattern is deliberately loose and may drop a few postings that merely
  contain a long number. This is a selection effect: 58 of the 71 boards had postings left, and a company that puts an
  address in every posting is absent from the sample altogether.
- **Pool:** 9,120 postings (Greenhouse 6,346, Ashby 2,383, Lever 391).
- **Draw:** `random.Random(20261122)`, proportional by ATS with the largest-remainder rule: Greenhouse 278, Ashby 105,
  Lever 17, from 47 boards. The pool is sorted by `(ats, board, external_id)` before drawing, so the order boards
  answered in cannot change the result. The 400 drawn rows are then shuffled with the same generator and the first 100
  are `dev`, the other 300 `test` (dev: 73 Greenhouse, 20 Ashby, 7 Lever; test: 205, 85 and 10).
- **Not reproducible later:** boards change every day, so running `build_sample.py` again gives a different pool. The
  committed file is the record, and the script refuses to overwrite it.

## How it was labelled

An agent read each of the 220 distinct `(location_raw, country_hint, remote_hint)` inputs in the sample and labelled
it from `GUIDELINE.md`, once, then copied the label to every row with that input, so equal inputs always have equal
labels. No location parser or gazetteer was run or read, and the index's own `country` and `city` columns were not
looked at. The guideline's rules and examples were written before the first row was read; ten cases they did not
settle were decided while labelling and are listed at the end of the guideline with their row counts.

## What the labels look like

| | Rows |
|---|---|
| Country labelled | 391 (US 204, IN 51, CA 21, GB 20, SG 15, JP 13, MX 11, IE 9, AU 7, 22 other countries 40) |
| Null country and null city | 9 (6 test, 3 dev) |
| City null with a country labelled | 59 (for example "Remote - United States", "California") |
| Country hint present | 117 |
| `remote_hint` true or false | 98 (70 true, 28 false) |

## Limits to keep in mind

- **Few unknowns.** Only 9 rows are null and null, 6 of them in the test split. With zero wrong guesses on 6 attempts
  the 95% upper bound is about 3/6, so this sample can show that the parser does not guess on the commonest unclear
  strings but cannot bound how often it guesses. A separate synthetic "unreadable strings" slice (labelled as
  synthetic, reported apart) would be the way to measure that.
- **No ambiguous bare city.** No real row names a city that exists in several countries without a clue (the
  "Cambridge" case in `GUIDELINE.md` rule 4), so that branch is covered by the guideline but not by this data.
- **United States heavy.** 51% of the rows are US; 14 countries have a single row.
- **One day's postings, three ATS products.** Nothing here speaks for other boards or other days.

## The test split is frozen

The 100 `dev` rows are for building and debugging the parser. The 300 `test` rows are for one recorded run. Tuning code
or rules against the test split is not allowed (AI_SYSTEM 12.1, rule 3), and a failing test row is reported, not
patched around. If review finds a wrong label, correct the label, say so in the commit message, and re-run; never change
the code to fit a label.

## Pre-registered bars

Written on 2026-11-23, before any run of `score.py` on the test split (AI_SYSTEM 12.1: fix the metric and the bar
before the run; a changed bar is a new, dated pre-registration).

On the 300 test rows, both must hold:

- `location_accuracy >= 0.95`. A row is correct when the predicted country equals `label_country` and the predicted
  city equals `label_city`. Cities are compared through GeoNames' names and alternate names (the suite's own reading of
  GeoNames, apart from the parser's tables), so "Bengaluru" matches the label "Bangalore" and "New York City" matches
  "New York". A null equals a null and nothing else.
- `location_guessed == 0`. A row is guessed when a label is null and the parser returned a value for it. Rows are
  counted, not fields.

Reported with counts and a 95% Wilson interval, always beside the words "labels drafted by an agent, review pending":
no figure from this sample is final until the maintainer has reviewed 60 of the 300 test rows. One recorded run, read
once; a miss is reported as a miss and the code is not tuned against these rows.

**What the guessed bar can and cannot show.** Only 6 of the 300 test rows are labelled null for both country and city,
so `location_guessed = 0` on them is weak evidence: with zero guesses in 6 attempts the 95% upper bound on the guess rate
is about 3/6 (AI_SYSTEM 12.1, rule 2). Rows with a country but a null city ("Remote - United States") are further
chances to guess a city, and the report counts them, but no row names an ambiguous city with nothing to settle it, so
that branch of the parser is covered by `backend/tests/ingest/test_location.py` and not by this data. Read the guessed
bar as "no guess on the strings the sample holds", not as a rate.

The harness gate reads `metrics.score`, so for this suite `score` is the accuracy, or 0 when any row was guessed; the
honest numbers are `location_accuracy` and `location_guessed`. The floor is in `evals/thresholds.toml`.

## Commands

```
uv run --project backend python evals/location/build_sample.py --check   # offline, validates sample.jsonl
uv run --project backend python evals/location/score.py --split dev      # development rows, with every miss
```

The test split, recorded (needs a migrated database; never the live one):

```
DATABASE_URL_WORKER=postgresql://app_worker:...@127.0.0.1:5433/<name>_test \
  uv run --project backend python evals/location/score.py --record --report evals/location/baseline.json
cd backend && python -m app.evals gate location_accuracy --run <the run id it printed>
```

`score.py` is not in the `SUITES` table of `backend/app/evals/runner.py`: that table is for suites replayed through a
model, and this one calls none, so `python -m app.evals run` does not know it. `score.py --record` writes the same
tables, and `gate` judges the run from them.

Building needs the internet and a read-only `DATABASE_URL`, and refuses to run while `sample.jsonl` exists.
