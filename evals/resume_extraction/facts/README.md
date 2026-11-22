# Resume facts: invented gold set, recorded replay, scorer

This folder holds the evaluation of resume extraction (`resume.facts.roles`, `resume.facts.education_skills`,
`resume.facts.summary`; AI_SYSTEM 4.2). It runs in the pull-request eval gate with no model key.

## Read this first: what the replay score is, and is not

The replay score exercises the code path only. The answers come from a fake provider: a script
(`record_fake.py`) wrote them from the gold truth, then damaged about 8% of the items on purpose. So the score shows
that the checks in code catch those faults and that the gateway, the checks, the scorer and the gate fit together. It is
**not a model quality number**, it says nothing about any model, and it must not be printed in the app, in a README or in
a report as a result (invariant 8). `baseline.json` records it so that a code change which lowers it fails the gate.

The targets in AI_SYSTEM 4.2 are 0.90 field-level F1 overall and 0.95 on employer, title and dates, on 200 to 300
resumes. They are measured on a live run with a real model, and the first live run is a manual step that has not been
done. This set has 120 resumes, which is below that size.

## Everything here is invented

The 120 resumes in `gold.jsonl` are synthetic. The people, employers, schools and cities are made up and belong to
nobody; the emails use the reserved domain `example.com` and the phone numbers sit in ranges set aside for fiction.
No real person's resume is here, and none may be added. The truth was written together with each resume by
`generate.py`, which also checks that every name, date and skill a fact claims is written in the lines it cites.

| Layout | Resumes | What the lines look like |
|---|---|---|
| `single` | 60 | one column, top to bottom |
| `two_column` | 30 | name and contact, then the left column (work), then a narrow right column that wraps its text |
| `sidebar` | 30 | the narrow sidebar first (contact, skills, education), then the main column |

An item is `{key, slice, synthetic, lines, protected_lines, expected: {facts}}`. `lines` are what the resume reader
returns for a clean file. A fact has `kind`, `line_start`, `line_end` and, as written in the resume, employer, title,
institution, degree or name; dates are normalised (`start_date`, `end_date`) and as written (`start_text`, `end_text`).
Six of the 120 have a `Date of birth` or `Marital status` line, which is no fact: extraction must decline it.

## Files

| File | What it is |
|---|---|
| `generate.py` | `--seed 7` writes the same `gold.jsonl` every time |
| `gold.jsonl` | the 120 invented resumes with their truth |
| `record_fake.py` | writes `recordings.jsonl`: the truth as the three tasks' JSON, plus a fixed fault injector |
| `recordings.jsonl` | one entry per resume and task: `{key, task, request, answers}` |
| `score.py` | the scorer; the Phase 1 runner calls its `run()` |
| `baseline.json` | the replay result of the committed recordings; `evals/thresholds.toml` takes its floor from it |

Each recording is keyed by the SHA-256 of the request it answers (task and messages, as rendered after the contact
details are masked). A change to a prompt in `backend/app/profile/extract/prompts/`, to the masking in
`backend/app/profile/resume/contact.py`, or to the gold set therefore makes the recordings stale: the run stops with
`RecordingStale` (status `error`, which the gate refuses) instead of replaying answers to a different question.

## The faults

About 8% of the items get one fault, chosen among those that fit: a line range that points at the wrong lines (it may
fall off the end of the resume), an employer, school or skill that the cited lines do not contain, or dates swapped so
they run backwards. The checks in `validate.py` report what they can as unverified; a wrong range on a bullet cannot be
told from a right one, so it stays kept and costs both precision and recall. A resume with a protected-characteristic
line also gets an extra item citing it, which must be dropped and counted as `protected_skipped`.

## Scoring

A kept fact is paired with the truth fact of its kind that it overlaps most, each truth fact used once. Every field of a
pair counts as a true positive when equal and as a false positive and a false negative when not; an unpaired kept fact
adds false positives and an unpaired truth fact false negatives. `kind` counts as one field of every fact. F1 is
`2 * tp / (2 * tp + fp + fn)` from whole numbers, so a replay gives the same score on every machine.

- `score` (what the gate reads) and `f1_overall`: all fields.
- `f1_employer_title_dates`: employer, title, start date and end date alone.
- `f1_by_slice`: the three layouts.
- `quote_exact_share`: the share of kept facts whose `source_quote` equals the text at their `source_span`. It must be
  1.0 and is enforced: one mismatch ends the run in error.

## Commands

```
# regenerate (only when the generator changes)
cd backend && uv run python ../evals/resume_extraction/facts/generate.py --seed 7

# re-record (after a prompt, masking or gold-set change)
cd backend && uv run python ../evals/resume_extraction/facts/record_fake.py

# replay through the gate; DATABASE_URL_WORKER must name a migrated database, never the real one
cd backend && python -m app.evals run resume_extraction
cd backend && python -m app.evals gate resume_extraction --run <id printed by run>

# rewrite baseline.json (needs a migrated scratch database whose name ends in _test)
cd backend && DATABASE_URL_WORKER=... uv run python ../evals/resume_extraction/facts/score.py
```

After rewriting `baseline.json`, set `min_score` for `resume_extraction` in `evals/thresholds.toml` to its `score`, and
state why in the commit. `backend/tests/profile/test_extract_gold.py` checks that the floor equals the baseline, that
the committed recordings reproduce it, and that a recording set with 35% faults fails the gate.
