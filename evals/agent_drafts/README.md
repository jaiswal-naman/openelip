# agent_drafts gold set (plan 07-02; CMP-03, CMP-04, CMP-05, AI_SYSTEM 6.6, 6.8, 6.9)

**Labels: agent-drafted, maintainer review pending.** 50 invented items: Writer 10, Chaser 10, Coach 30 (the Coach target
is 30; Writer and Chaser are samples). The people, employers, companies and numbers are made up, and the scores of a
replay check wiring only: they are never published (invariant 8).

## What it measures

Three invented candidates (a data engineer, a learning designer, an operations analyst) with five confirmed facts each,
a four-requirement job with stored verdicts and a three-claim company dossier. Each item holds one scripted model draft with
**seeded temptations** and a label per sentence or item (`supported` or `unsupported`, with the expected reason). The suite
sends the real request through the real gateway (fake provider), then the real validators, and checks what is kept.

| Slice | Items | Validators | Temptations seeded |
|---|---|---|---|
| Writer | 10 (7 campaign packets with a dossier, 3 without) | `apply.packet.check_draft` and `assemble`, layers 1 and 2 | a changed number, an unknown fact alias, a skill the facts lack, a dossier claim with a changed number, an unknown dossier claim, an overclaim only `verify.entail` rejects, a sentence citing nothing, a drafted pay, notice, start date or work authorisation answer |
| Chaser | 10 | `chaser.validate` | a changed number, an email, a link, pay, availability, an uncited skill, an unknown alias, a changed employer, an inflated title, an overclaim |
| Coach | 30 | `coach.validate` | a story without a fact, a framing with a new number, a framing that overclaims, an unknown requirement, an unknown fact, a question to ask the company without a dossier claim, with a changed number, or citing an unknown claim |

## Pre-registered bars (written before the first run)

| Metric | Bar |
|---|---|
| `drafts_unsupported_passed` | 0: no sentence or item labelled unsupported is kept |
| `drafts_supported_kept_rate` | at least 0.90 of the items labelled supported are kept |
| `drafts_candidate_only_blank` | 1.0: pay, notice, start date and work authorisation stay blank in every Writer packet |
| `prep_items_without_fact` | 0: no kept Coach story without a fact (a gap item may have none), no kept question to ask without a claim |
| `drafts_restricted_passed` | 0: no kept sentence contains an email, link, phone number, salary, notice or availability (the scorer's own test) |
| `score` | 1.0 only when every bar holds, else 0.0. Floor in `evals/thresholds.toml`: 1.0 |

## Degraded fixture

`degraded/pass_through.json` is a flag the scorer reads: it replays the same recorded drafts with every validator switched
to pass-through (layer 1, `verify.entail`, the restricted-content and citation tests). The flag exists only in this scorer.
The run scores 0.0 and the gate fails. The tests also switch single checks off and watch the matching bar fail.

## Files

`build.py` writes `gold.jsonl` and `recorded/` from fixed rotations (`uv run python ../evals/agent_drafts/build.py` from
`backend/`); a test rebuilds it in a temporary folder and compares. `recorded/<key>.json` is the draft and then, when
sentences reach layer 2, the `verify.entail` answer. `score.py` is the scorer; `GUIDELINE.md` is the labelling guide.

```
cd backend
uv run python -m app.evals run agent_drafts         # a replay needs LLM_PROVIDER=fake and a scratch database
uv run python -m app.evals gate agent_drafts --run <the id printed last>
```
