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

## Commands

```
uv run --project backend python evals/location/build_sample.py --check   # offline, validates sample.jsonl
```

Building needs the internet and a read-only `DATABASE_URL`, and refuses to run while `sample.jsonl` exists.
