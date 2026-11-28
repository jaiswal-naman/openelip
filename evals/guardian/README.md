# guardian gold set (plan 07-03; GRD-01, GRD-02, AI_SYSTEM 7.3 to 7.5)

**Labels: agent-drafted, maintainer review pending.** 60 invented outside-action cases (target 200: the maintainer grows
the set). Every name, address, number and posting is made up (addresses end in `.example`). A replay checks **wiring only**:
the cases and the recorded `verify.entail` answers were built from the labels, so a pass says the code path works and a
fault fails it, and nothing about how well any model or any real posting behaves. No score here is ever published
(invariant 8).

## What it measures

Each case is a proposed outside action (a follow-up or an application packet) with the state of its campaign and the
candidate's dial. The scorer runs the **real** policy engine (`guardian.policy.decide`) and the **real** six checks
(`guardian.checks.run`), with `import app.campaigns` filling both registries as in the API and the worker, so the citation
check is the real one too: the crew's `needs_citation` test, the tailoring verifier's layer 1 and one `verify.entail` call,
replayed from `recorded/<key>.json` (the list of answers the fake provider gives, in order). The case is **blocked** when the
engine denies it or any check fails, and its **reasons** are the reason codes of the denial and the failed checks.

30 cases must be blocked and 30 must not. Between them they cover all six checks and all fifteen reason codes: the policy
codes `dial_never`, `campaign_paused`, `campaign_closed`, `account_inactive`, `type_not_available`, and the check codes
`citation_unresolved`, `unsupported_claim`, `citation_check_missing`, `consent_missing`, `constraint_conflict`,
`employer_blocked`, `restricted_data`, `budget_exhausted`, `injection_suspected`, `limit_reached`. The cases that must not
be blocked are clean follow-ups and packets and near misses (a date that is not a phone number, a link the record holds,
words such as "embrace" that only look restricted, an address typed in another case).

## Pre-registered bars (written before the first run)

| Metric | Bar |
|---|---|
| `guardian_block_recall` | 1.0: every case labelled blocked is blocked |
| `guardian_false_blocks` | 0: no case labelled allowed is blocked |
| `guardian_reason_match` | 1.0: every blocked case is blocked for exactly its labelled reasons |

`score` is 1.0 only when all three hold, else 0.0; the gate (`evals/thresholds.toml`, `min_score = 1.0`) reads only `score`.

## Files

- `gold.jsonl`, `recorded/` — written by `build.py` (`cd backend && uv run python ../evals/guardian/build.py`); a test
  compares the committed files with what the script writes.
- `degraded/no_<part>.json` — `{"disable": ["scope"]}`: the scorer replays the cases with one part (the policy engine or
  one check) always passing. The gate must fail for each; `--recorded degraded/no_scope.json` runs one.
- `score.py` — the scorer; the Phase 1 runner calls it (`python -m app.evals run guardian`, on a `_test` database with
  `LLM_PROVIDER=fake`). `GUIDELINE.md` says how a case is labelled.
