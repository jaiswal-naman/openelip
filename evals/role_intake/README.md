# Role intake: gold set, recorded replay, scorer

This folder grades how an employer's role description is read into requirement items: the `job.requirements.extract`
task, which the hiring desk runs on the text an organization member writes for a role (HIR-02, AI_SYSTEM 4.3). The task,
its prompt, its route and its validator are the ones that read public job postings (Phase 2); what is new here is the
text they are graded on: invented role descriptions written the way employers write them. It runs in the pull-request
eval gate with no model key.

## Read this first: what the replay score is, and is not

The replay score exercises the code path only. The recorded answers are built from the gold labels (each answer cites
the lines of its description that hold the labelled items and quotes them), so a pass shows that the segmenter, the
gateway, the validator that computes every span, the scorer and the gate fit together, and that a recording with
injected faults fails. It is **not a model quality number**, it says nothing about any model, and it must not be printed
in the app, in a README or in a report as a result (invariant 8). How well a model reads employer text comes only from
the manual checks at the end.

## Status of the labels

**Labels drafted by an agent, maintainer review pending.** Every description says
`labels: "agent-drafted, maintainer review pending"`. All 30 descriptions are invented: the organizations all end in
"Demo", and no description comes from a real employer, a real posting or a real person. The maintainer reviews the
labels, a second person labels a random 20% of the descriptions from `GUIDELINE.md` without looking at them, agreement
is reported, and disagreements amend the guideline. The set is meant to grow toward 50 descriptions (06-INDEX, manual
check 1); 30 ship now, because hand labelling is the limit. A score on 30 descriptions carries a wide interval and is
reported with its counts and a Wilson interval (AI_SYSTEM 12.1), never as a bare percentage.

## Where the descriptions came from

An agent wrote all 30 for this set, one for each of 30 invented organizations ("Halden Demo", "Brightwell Demo", ...),
and then drafted the labels from `GUIDELINE.md`. Nothing is copied from a real posting or a real employer, and no person
is named. The roles are ordinary ones (a nurse, a bookkeeper, a barista, an electrician, a data analyst, a principal
engineer, ...) so that the set is not only engineering. The text is varied on purpose, the way employers write:

| Slice | Descriptions | What it is |
|---|---|---|
| `prose` | 6 | one paragraph, the requirements in running sentences |
| `bullets` | 6 | headings and bullet or numbered lists, with different bullet marks; one bullet wrapped over two lines |
| `mixed` | 4 | prose, headings, a duties list that hides a requirement, `Required:` and `Preferred:` lines, semicolon lists |
| `short` | 3 | one to three lines; one states no requirement at all |
| `long` | 3 | a page with the company's story, benefits, how to apply and an equal-opportunity notice around the requirements |
| `non_ascii` | 3 | accents, curly quotes, dashes, CJK, and emoji outside the Basic Multilingual Plane ahead of the first item |
| `injection` | 2 | a sentence addressed to a model ("ignore your earlier instructions ..."), which must not become an item |
| `years` | 3 | many typed numbers of years: `5+`, `3-5`, `at least 2`, `1 to 2`, `up to 3`, and clauses with their own figure |

No extractor, no heading rule and no model was run or read while the descriptions were written and labelled.

## What the labels cover

| | Count |
|---|---|
| Descriptions | 30; 127 to 1,736 characters, median 421; 10 contain non-ASCII text; 1 has no requirement |
| Items | 155: 115 required, 40 preferred; 0 to 9 per description, median 5.5; the items cover about half of the text |
| Kinds | skill 51, experience 41, certification 19, other 19, education 10, location 9, authorization 6 (all seven kinds) |
| Items that state a number of years | 21 (`min_years` set) |
| Spans | 2 descriptions have a symbol outside the Basic Multilingual Plane before their first item, so a span counted in UTF-16 would be wrong; 1 item's quote contains a line break |
| Preferred items | 28 of the 30 descriptions have at least one |

## Limits to keep in mind

- **Thirty is a start, not the target.** The set is meant to grow toward 50 with a second rater on 20%. A score on 30
  descriptions carries a wide interval.
- **One labeller, no second rater.** The eleven decisions at the end of `GUIDELINE.md` are the judgement calls.
- **Invented text is tidier than real text.** The agent's descriptions have few typos, no pasted markup, no half-English
  mixtures and none is longer than 1,736 characters. Real employers write messier and longer text, so a live score here
  would read better than one on real role descriptions.
- **English only**, apart from a few words of Japanese and German.
- **Nearly every description has a "nice to have".** Only two have none, so a model that marks too many items preferred
  is hard to catch here, and one that marks too few is easy to catch.
- **The two injection descriptions test the labels, not a defence.** They show that the sentence is not an item; the
  prompt-injection suite of Phase 5 is where defences are measured.
- **No dev and test split.** Nothing is tuned on this set: the prompt is the one that reads postings. If it is ever tuned
  for employer text, grow the set and freeze a test split first.

## Pre-registered bars

Written on 2026-11-23 (AI_SYSTEM 12.1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). No run of this suite existed when these bars were written: this file, `GUIDELINE.md` and the
`[role_intake]` line of `evals/thresholds.toml` are the first files of the suite in git history, committed before
`gold.jsonl`, any script and any recording. Since then only the parts that describe the finished gold set were added
(the files table, where the descriptions came from, what the labels cover, the limits) and the guideline's "Decisions
made while labelling"; the bars and the guideline's rules are as first committed.

**What is scored.** A run replays the recording over every description through the real task path: the segmenter, the
gateway's fake provider, then the validator, which computes each item's text and span from the description. Items the
validator turns away are counted and never become rows. The scorer is the one of the requirement-extraction suite
(`evals/requirement_extraction/score.py`), used as it is.

- `req_item_f1`: predicted items and gold items are paired one to one, the pair with the greatest overlap first, where
  overlap is intersection over union of the two spans (in code points); a pair is a match at 0.5 or more. Precision is
  matches over predicted items, recall is matches over gold items, and `req_item_f1` is their harmonic mean. Precision
  and recall are reported with 95% Wilson intervals.
- `req_importance_accuracy`: of the matched pairs, the share whose `required` or `preferred` agrees, with its Wilson
  interval.
- `req_spans_resolved`: the share of predicted rows whose text is exactly the description's text at their span.

**The bars.**

- `req_spans_resolved = 1.0`. Absolute: a stored citation that does not resolve is a blocker whatever the other numbers
  are (AI_SYSTEM 12.8).
- `req_item_f1 >= 0.80`
- `req_importance_accuracy >= 0.80`

`gate.py` compares only `metrics.score` with the floor, so `score` is 1.0 when every bar holds and 0.0 otherwise, and
`evals/thresholds.toml` sets `min_score = 1.0`. The three metrics, their counts and intervals, and the bars are recorded
beside it.

**What the bars show.** These are bars for a replay, which is a wiring check: low enough that a correct code path
passes and high enough that a damaged recording fails. They are not targets for a model. Replaying a recording built
from the gold labels must give 1.0 for all three metrics, so `score` is 1.0: any other number means the code path broke.
The degraded recording (the importance of a third of the items turned around, and one item whose quote is not in the
description) must give an importance accuracy below 0.80, score 0.0, and fail the gate. Neither says anything about a
model. The targets for a live model run are the ones of AI_SYSTEM 4.3 (`req_item_f1 >= 0.85`,
`req_importance_accuracy >= 0.90`, the same as for postings); applying them in code is part of recording the first live
run, which needs a model key and is a manual check.

## Commands

```
cd backend

# replay the recording, record the run, gate it (what CI does; needs a migrated scratch database)
uv run python -m app.evals run role_intake
uv run python -m app.evals gate role_intake --run <the id printed last>
```

A recorded run is frozen and cannot be deleted, and `python -m app.evals` writes to whichever database
`DATABASE_URL_WORKER` names, whose default is the live one. Run it by hand only with `DATABASE_URL_MIGRATE`,
`DATABASE_URL_API` and `DATABASE_URL_WORKER` all pointing at a scratch database whose name ends in `_test` (roles `app`,
`app_api` and `app_worker`), and `LLM_PROVIDER=fake`.

## Manual checks (they need the maintainer or a model key)

1. The maintainer reviews the labels; a second rater labels 20% of the descriptions; agreement is reported; the
   guideline is amended; the set grows toward 50.
2. With a model key: a live run (`LLM_PROVIDER=anthropic`), reported with counts and Wilson intervals and against the
   AI_SYSTEM 4.3 targets. Until then no number of this suite is a quality number.
