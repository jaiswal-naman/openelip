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
