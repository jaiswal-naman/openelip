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
