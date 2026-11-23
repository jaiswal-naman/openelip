# Evidence faithfulness: gold set, recorded replay, scorer

This folder grades the match evidence task (`match.evidence.score`, AI_SYSTEM 4.4): for one candidate's record and one
job posting, does each requirement get the verdict the record supports, citing facts that exist? It runs in the
pull-request eval gate with no model key.

## Read this first: what the replay score is, and is not

The replay score exercises the code path only. The recorded answers are built from the gold labels (each answer is the
expected verdicts of its pair), so a pass shows that the request builder, the gateway, the validator, the scorer and the
gate fit together, and that a recording with injected faults fails. It is **not a model quality number**, it says
nothing about any model, and it must not be printed in the app, in a README or in a report as a result (invariant 8).
Faithfulness as people judge it, and any number for a real model, come only from the manual checks at the end.

## Status of the labels

**Labels drafted by an agent, maintainer review pending.** Every pair says
`labels: "agent-drafted, maintainer review pending"`. The maintainer reviews the labels, a second person labels a random
20% of the pairs from `GUIDELINE.md` without looking at them, agreement is reported as Cohen's kappa, and disagreements
amend the guideline. The target is 150 pairs (AI_SYSTEM 12.2); 30 ship now, because hand labelling is the limit. A score
on 30 pairs carries a wide interval and is reported with its counts and a Wilson interval (AI_SYSTEM 12.1), never as a
bare percentage.

## Files

| File | What it is |
|---|---|
| `GUIDELINE.md` | The four verdicts, what counts as stated, how years are judged against lengths counted by code, worked examples, decisions made while labelling. |
| `labels.json` | The hand-drafted verdicts: for each pair the resume, the posting and, for every requirement, a verdict, the facts it cites and a reason. |
| `build_gold.py` | Writes `gold.jsonl` from `labels.json` and the two gold sets below; `--check` proves it is current; `--show` prints the sheet a labeller works from. |
| `gold.jsonl` | The 30 pairs, as the scorer reads them (see below). Written by `build_gold.py`, never by hand. |
| `recorded.json` | The replay: one answer per pair in pair order, equal to the expected verdicts. The suite test proves it. |
| `fixtures/degraded.json` | A damaged copy of the replay that the gate must fail. |
| `score.py` | The scorer; the Phase 1 runner calls its `run()` (`evidence_faithfulness` is an `OwnScorer` entry in `SUITES`). |

A gold item is `{key, slice, resume_ref, posting_ref, as_of, facts, requirements, expected, labels}`. `facts` are rows
shaped like `profile.api.usable_facts` and `requirements` rows shaped like `ingest.api.get_job`'s, written in the order
`build_request` numbers them, so `f1` is the first fact in the list and `r1` the first requirement. `expected.verdicts`
holds, for every requirement, `{requirement, verdict, facts, why}`; `why` is the labeller's reason and is never part of a
recorded answer. `slice` is the requirement kind mix of the pair (`skill_led`, `experience_led` or `mixed`). `as_of` is
the date a current role is counted to.

## Where the pairs came from

- **Resumes:** ten of the 120 invented resumes of `evals/resume_extraction/facts/gold.jsonl` (Phase 3). The people,
  employers and schools are made up; the facts are that gold set's truth, turned into confirmed-fact rows with the spans
  that resolve in the resume text. No real person's resume is here, and none may be added.
- **Postings:** thirty of the 50 real public postings of `evals/requirement_extraction/gold.jsonl` (Phase 2), three for
  each resume, with the requirement items and spans that gold set already holds. Phase 2's rule applies: no posting with
  an email address or phone number. The text is third-party text that companies published on their own job boards.
- **Pairs:** about a third are deliberately poor fits (a business posting for an engineer's record and the other way
  round), so that most verdicts are not `met`, and every verdict and every requirement kind the Phase 2 labels contain
  appears.

## Pre-registered bars

Written on 2026-11-23 (AI_SYSTEM 12.1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). No run of this suite existed when these bars were written: this file and `GUIDELINE.md` are the first
files of the suite in git history, committed before `gold.jsonl`, `score.py` and any recording.

**What is scored.** A run replays the recording over every pair: `build_request`, the gateway's fake provider, then
`validate`, and one fresh call when the validator rejects the first answer (AI_SYSTEM 4.4: a rejected set gets one
retry). A pair is *accepted* when either answer passes the validator and *rejected twice* when both fail it. An answer
that does not fit the schema twice at the gateway makes the pair fail and is counted as `schema_invalid_pairs`.

- `evidence_faithful_rate`: verdicts equal to the gold verdict, over all verdicts of accepted pairs.
- `false_met_required_rate`: required requirements answered `met` whose gold verdict is `not_met` or `no_evidence`,
  over all required requirements of accepted pairs.
- `citation_reject_rate`: pairs rejected twice, over all pairs.

**The bars** (AI_SYSTEM 4.4 targets, and its 2% alert line for rejections):

- `evidence_faithful_rate >= 0.90`
- `false_met_required_rate <= 0.05`
- `citation_reject_rate <= 0.02`

`gate.py` compares only `metrics.score` with the floor, so `score` is 1.0 when every bar holds and 0.0 otherwise, and
`evals/thresholds.toml` sets `min_score = 1.0`. The three rates, their counts and the bars are recorded beside it.

**What the bars show.** Replaying a recording built from the gold labels must give 1.0, 0.0 and 0.0, so `score` is 1.0:
any other number means the code path broke. `fixtures/degraded.json` (five required `no_evidence` requirements answered
`met` with an unrelated fact, and two pairs whose answers cite an alias that was not sent, twice each) must give a lower
faithful rate, a higher false-`met` rate and a higher rejection rate, score 0.0, and fail the gate. Neither says
anything about a model. The first live run is the real test of these bars.

## Commands

```
cd backend

# replay the recording, record the run, gate it (what CI does; needs a migrated scratch database)
uv run python -m app.evals run evidence_faithfulness
uv run python -m app.evals gate evidence_faithfulness --run <the id printed last>

# rebuild or check the gold set
uv run python ../evals/evidence_faithfulness/build_gold.py [--check]
```

`--recorded FILE` replays another recording of the same suite. A recorded run is frozen and cannot be deleted, so these
commands write only to a database named `..._test` or `evals`.

## Manual checks (they need the maintainer or a model key)

1. The maintainer reviews the labels; a second rater labels 20% of the pairs; kappa is reported; the guideline is amended.
2. With a model key: a live run of the suite (`LLM_PROVIDER=anthropic`), reported with counts and Wilson intervals, and
   the counterfactual sample (remove the cited fact and score again; the verdict should fall to `no_evidence`).
3. Humans judge the faithfulness of a sample of live verdicts (AI_SYSTEM 4.4: at least 90% faithful).
4. Grow the set toward 150 pairs and about 1,500 verdicts, with every requirement kind (the Phase 2 labels hold no
   `authorization` requirement, so none appears here).
