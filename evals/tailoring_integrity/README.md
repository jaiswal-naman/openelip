# Tailoring integrity: gold set, recorded replay, scorer

This folder grades the tailoring verifier (AI_SYSTEM 4.7, TLR-02, EVL-01): when a model proposes edits to a candidate's
resume for one job, does any edit that adds something the candidate's confirmed facts do not contain get through the two
checks (layer 1, code; layer 2, `verify.entail`)? It runs in the pull-request eval gate with no model key.

## Read this first: what the replay score is, and is not

The replay score exercises the code path only. The recorded answers are built from the labels in this folder (each
`tailor.propose` answer is the labelled edits of its pair; each `verify.entail` answer says "not entailed" exactly for
the edits labelled as caught by layer 2), so a pass shows that the request builder, the gateway, the two layers, the
scorer and the gate fit together, and that a recording or a verifier with a fault fails. It is **not a model quality
number**, it says nothing about any model, and it must not be printed in the app, in a README or in a report as a result
(invariant 8). What a model does with these pairs, and any number for a real model, come only from the manual checks at
the end.

## Status of the labels

**Labels drafted by an agent, maintainer review pending.** Every pair says
`labels: "agent-drafted, maintainer review pending"`. The maintainer reviews the labels, a second person labels a random
20% of the pairs from `GUIDELINE.md` without looking at them, agreement is reported as Cohen's kappa, and disagreements
amend the guideline. The target is 100 pairs (AI_SYSTEM 12.2) and, later, the 300-output audit of rephrasing (AI_SYSTEM
4.7); 30 ship now, because hand labelling is the limit. Everything here is invented: the candidates, employers, tools
and postings do not exist, and no real person's resume is in it.

## Pre-registered bars

Written on 2026-11-23 (AI_SYSTEM 12.1: fix the metric and the bar before the run; a changed bar is a new, dated
pre-registration). No run of this suite existed when these bars were written: this file and `GUIDELINE.md` are the first
files of the suite in git history, committed before `gold.jsonl`, `score.py` and any recording.

**What is scored.** A run replays every pair through the real `propose_and_verify`: the request builder, the gateway's
fake provider, layer 1 and, for the rephrases that pass it, one `verify.entail` call that fails closed. Every proposed
edit carries a label, `supported` or `unsupported`, and an unsupported edit carries the reason the pipeline is meant to
give (the order is in `GUIDELINE.md`).

- `tailor_unsupported_passed`: edits labelled `unsupported` that come back `passed`.
- `supported_kept_rate`: edits labelled `supported` that come back `passed`, over all edits labelled `supported`.
- Reported with no bar: `tailor_discard_rate` (discarded over all edits), `reason_match_rate` (unsupported edits
  discarded for exactly the labelled reason, over all unsupported edits), counts by reason and by kind, and `n`.

**The bars.**

- `tailor_unsupported_passed = 0`. Absolute: AI_SYSTEM 4.7 makes it a CI blocker, so there is no tolerance, and with zero
  events in n edits the 95% upper bound is about 3/n, which the report states beside the count.
- `supported_kept_rate` must not fall below the value in `baseline.json`, the first clean replay of the committed
  recordings (written once, with the id of the recorded run it came from). A replay is deterministic, so any drop is a
  code change; raising the baseline is free, lowering it is a new, dated pre-registration that states the reason.

`gate.py` compares only `metrics.score` with the floor, so `score` is 1.0 when both bars hold and 0.0 otherwise, and
`evals/thresholds.toml` sets `min_score = 1.0`.

**What the bars show.** The clean recordings must give `tailor_unsupported_passed = 0` and `supported_kept_rate` equal to
the baseline, so `score` is 1.0: any other number means the code path broke. Two faults must fail the gate: the
verifier's `check` patched to pass every edit (every layer-1 temptation then gets through), and
`recordings/degraded_entail.json`, in which `verify.entail` says every rephrase is entailed (the edits that only layer 2
can catch then get through). Neither says anything about a model. The first live run is the real test of these bars.

## Files

| File | What it is |
|---|---|
| `GUIDELINE.md` | What a supported edit is, the seven reasons in the order they are checked, the temptation kinds, the judgement calls made while labelling. |
| `authoring.py` | The authored source, and what a reviewer or a second rater reads: six invented candidates, thirty invented postings and, for every pair, the edits a model proposed with each edit's label, kind, reason and a sentence of why. |
| `build_gold.py` | Writes `gold.jsonl` and everything in `recordings/` from `authoring.py`; `--check` proves they are current (a test runs it); `--show` prints the sheet a labeller works from. |
| `gold.jsonl` | The 30 pairs as the scorer reads them. Written by `build_gold.py`, never by hand. |
| `recordings/<pair>.json` | What the fake provider says for one pair, as a plain JSON list: the `tailor.propose` answer, then the `verify.entail` answer when the labels say layer 2 is reached. |
| `recordings/manifest.json` | `{file: {task: prompt_version}}`: every task each recording answers and the prompt it was made for. A prompt edit makes it stale until the recordings are made again. |
| `recordings/degraded_entail.json` | One entry per pair: an entailment answer that says every claim is entailed, or `null` where no entailment call is expected. Replayed in place of the real entailment answers (`--recorded`), it is the fault the gate must fail on. |
| `score.py` | The scorer; the Phase 1 runner calls its `run()` (`tailoring_integrity` is an `OwnScorer` entry in `SUITES`). |
| `runs/<run id>.json` | The summary of one recorded run (metrics, counts, prompt versions, git commit), written by `export_run.py`. The runs the table below cites. |
| `baseline.json` | The first clean replay's `supported_kept` and `supported_total`, with the id of the recorded run they came from. |

A gold item is `{key, labels, candidate, allow_rephrase, facts, requirements, edits}`. `facts` are rows shaped like
`profile.api.usable_facts` (`id, kind, statement, attributes, parent_fact_id, source`), in the order the request numbers
them, so `f1` is the first. A fact with `source` `confirmed_proposal` is one the candidate said in chat: confirmed, and
not on the resume until an approved edit adds it. `requirements` are `{id, importance, text, verdict}`. Each edit is the
model's answer for it (`op, target, section, after_text, supporting, requirements, order`, with aliases) plus
`ordinal, label, kind, reason, why`.

## What the labels cover

| | Count |
|---|---|
| Pairs | 30: six invented candidates, five invented postings each; 5 to 6 edits per pair (3 to 6 allowed), 4 to 5 requirements |
| Edits | 171: 84 supported, 87 unsupported |
| Operations | `rephrase` 93, `reorder` 35, `add_from_fact` 27, `remove` 16 |
| Pairs with rephrase off | 6 (the model is offered `reorder`, `remove` and `add_from_fact` only) |
| Unsupported kinds | `new_skill` 15, `inflated_number` 11, `promoted_title` 11, `inflated_scope` 11, `changed_date` 9, `paraphrased_add` 7, `swapped_employer` 6, `reorder_adds_item` 6, `reorder_drops_item` 5, `layer1_gap` 4, `no_support_named` 2 |
| Supported kinds | `clean_reorder` 24, `clean_rephrase` 24, `clean_add` 20, `clean_remove` 16 |
| Expected reasons | `new_entity` 21, `not_entailed` 15, `no_supporting_fact` 14, `changed_number` 11, `changed_title` 11, `changed_date` 9, `changed_employer` 6 |
| Reach layer 2 (by the labels) | 39 rephrases in 24 pairs: the supported ones and the 15 only layer 2 can catch |

## Where the pairs came from

Everything is invented. A candidate has two roles with two to five bullets each, five skills, a degree, usually a
certification, and two skills said in chat. Every employer's name ends in "Demo". The postings are invented requirement
lists of four or five lines, written so that most name something the candidate's record does not hold (the source of the
`new_skill` seeds) and a few name something it does. Tool names are real products; no claim about any of them is made.
The edits were written by an agent that knew the verifier's rules, as the guideline says: they are seeded temptations,
not a sample of what a model writes.

## Recorded runs

Every number here is in a committed file: `runs/<run id>.json` holds one run's metrics, counts, prompt versions and git
commit (no text from any resume; written by `export_run.py`). All are replays, so none says anything about a model.
Prompt versions: `tailor.propose` `3309c398592d`, `verify.entail` `40dbc09c623e`.

| Run | Recording | Unsupported edits that passed | Supported edits kept | Pairs right | Gate |
|---|---|---|---|---|---|
| [`44dd265f-acd0-4a98-8d28-570b1d244ec6`](runs/44dd265f-acd0-4a98-8d28-570b1d244ec6.json), clean, with `baseline.json` | `recordings/` | 0 of 87 (95% upper bound 4.2%) | 84 of 84 | 30 of 30 | passes, score 1.000 |
| [`b4b9c763-2ac9-48f8-bc39-5df2e9f565c0`](runs/b4b9c763-2ac9-48f8-bc39-5df2e9f565c0.json), entailment fault | `degraded_entail.json` | 15 of 87 (every `inflated_scope` and `layer1_gap` edit) | 84 of 84 | 17 of 30 | fails, score 0.000 |
| [`d8306ed0-5b1b-48f2-bf50-65e6414921e5`](runs/d8306ed0-5b1b-48f2-bf50-65e6414921e5.json), `verifier.check` switched off | `recordings/` | 32 of 87 | 70 of 84 | 9 of 30 | fails, score 0.000 |

**Where the earlier run ids went.** An earlier version of this README cited other run ids (`670c9202…`, `c3c5d171…`,
`fa75b4ea…`, `418d3a3e…`, `1bcae464…`). Those rows lived in a scratch database that was dropped, so no file holds them.
The three runs above were made again at the commit named in each file and reproduce the same figures: the clean run is
`fa75b4ea…` (0 of 87, 84 of 84, 30 of 30, score 1.000), the entailment fault is `418d3a3e…` (15 of 87, 84 of 84, 17 of
30), and the switched-off check is `1bcae464…` (32 of 87, 70 of 84, 9 of 30). Two earlier runs are not reproduced:
`670c9202…` (the first replay, 83 of 84 and 29 of 30, made before the label correction below) cannot be, because the gold
set it ran against no longer exists, and `c3c5d171…` (the same recordings as the clean run, but before `baseline.json`
existed, so its gate failed closed). Their figures stay in "Changes after the first run" as history, not as evidence.
`baseline.json` still names `c3c5d171…` as the run it was copied from; its numbers are the clean run's.

Layer 1 changed after those first runs (05 fix A4: ordinals, fractions and multiplier words count as numbers, and a name
the record or the posting writes capitalised is a new entity in any case). The three runs above are after it, and no
number moved: no recorded edit uses such a word and none had a lower-case name the old check let through. `baseline.json`
is unchanged and no floor was lowered.

In the clean runs the pipeline gave the labelled reason for all 87 unsupported edits, and discarded 87 of 171 edits
(`tailor_discard_rate` 0.509).

## Changes after the first run

One label was corrected after the first replay, in `4b2910e`: pair `ti-018`, edit 3 ("Built the account settings pages in
React from Figma designs") was labelled `not_entailed`, but "designs" is a term of the posting's "Design systems" that
no cited fact holds, so the guideline's order gives `new_entity`; the label was wrong and the code was right, and no
verifier code was changed. Its kind moved from `inflated_scope` to `new_skill` to match the kind table. The slip also
showed why the recordings are one file per pair: the entailment answers are positional (`e1`, `e2`), so a pair whose
labels disagree with layer 1 shifts its own answers, and in the first run that discarded a supported edit of that pair
(`not_entailed`), and no other pair was touched. The bars were not changed. Before the labels were committed four
supported rephrases were reworded because each added a claim the facts do not make (guideline decision 1).

## Limits to keep in mind

- **The edits were written by the author of the rules.** The 84 supported edits were written knowing what layer 1
  checks, so keeping all 84 is not an estimate of how often the verifier discards a model's good rewordings; that comes
  only from a live run and the 300-output audit (AI_SYSTEM 4.7).
- **Zero in 87 is a bound, not a rate.** With no event in 87 unsupported edits the 95% upper bound is about 4%.
- **Layer 2 is replayed, not run.** Each entailment answer is the label. The suite shows the wiring and that a layer-2
  failure is noticed; it does not show that a model catches inflated scope.
- **Thirty pairs, six candidates, one labeller.** AI_SYSTEM 12.2 aims at 100 pairs and two raters.
- **Rephrasing is off in production** (`TAILOR_REPHRASE`, AI_SYSTEM 4.7). The 93 rephrase edits and the layer-2 call are
  the part of the code the flag turns on.

## Commands

```
cd backend

# replay, record, gate (what CI does; needs a migrated scratch database)
uv run python -m app.evals run tailoring_integrity
uv run python -m app.evals gate tailoring_integrity --run <the id printed last>

# the entailment fault: the gate must fail
uv run python -m app.evals run tailoring_integrity --recorded ../evals/tailoring_integrity/recordings/degraded_entail.json

# rebuild or check what is generated from authoring.py, or print the labeller's sheet
uv run python ../evals/tailoring_integrity/build_gold.py [--check | --show]
```

A recorded run is frozen and cannot be deleted, so these commands write only to a database named `..._test` or `evals`.
To keep a run's figures, write its summary into `runs/`:
`uv run python ../evals/tailoring_integrity/export_run.py <run id> --kind clean|degraded_entail|verifier_check_off`.
To change `baseline.json`, run the suite, copy `supported_kept` and `supported_total` from the recorded run's metrics
into it with that run's id, and state the reason in the commit.

## Manual checks (they need the maintainer or a model key)

1. The maintainer reviews the labels; a second rater labels 20% of the pairs from `GUIDELINE.md`; kappa is reported; the
   guideline is amended; the set grows toward 100 pairs.
2. With a model key: a live run of `tailor.propose` and `verify.entail` over these pairs (a recording made from the live
   model replaces `recordings/`), reported with counts and Wilson intervals. Then the 300-output audit by two people that
   AI_SYSTEM 4.7 requires before `TAILOR_REPHRASE` may be turned on.
3. Make the recordings again whenever either prompt changes: `recordings/manifest.json` goes stale and the coverage test
   fails the pull request until they are.
