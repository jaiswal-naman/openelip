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
