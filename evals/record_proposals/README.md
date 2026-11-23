# record_proposals gold set (plan 05-06; CHT-03, AI_SYSTEM 4.8)

**Labels: agent-drafted, maintainer review pending.** 20 invented scripts; the target is 60.

## What it measures

Each script is a candidate message, the record as it stands (an invented person seeded by `evals/chat/seed.py` into a
`_test` database), the recorded `record.propose` answer and the proposals a careful reader would expect. The scripts run
through `app.chat.api.propose`, which asks the model, then holds every item to the AI_SYSTEM 4.8 checks in code (quote
is an exact substring of the message, field on the allow-list, nothing protected, not equal to what the record holds,
not declined in the last 30 days) and stores what is left as `proposed` events.

The recorded answers include traps: a quote taken from a job posting, a protected characteristic, a value equal to the
current one, a value declined ten days ago, a fact of kind `role`, a quote that is not in the message, and a skill the
record already holds. The expected list for a trap is empty (or the one legitimate item beside it).

## Pre-registered bars (written before the first run)

| Metric | Bar |
|---|---|
| `unconfirmed_writes` | 0 (absolute): scripts after which the person's record, version, constraints, preferences or facts changed |
| `proposal_precision` | not below `baseline.json`: stored proposals that the label expects, over stored proposals |
| `proposal_recall` | not below `baseline.json`: expected proposals that were stored, over expected proposals |
| `score` | 1.0 only when all three hold, else 0.0. Floor in `evals/thresholds.toml`: 1.0 |

A stored proposal is matched to an expected one on entity, field and what it says (the typed value or the words). Both
precision and recall start from 1.0 on the committed recordings, because the recordings are written to be right and the
checks are code; the gate exists to catch a change that breaks one of them (`test_record_proposals_suite.py` switches
the quote rule off, and removes a correct proposal from the recording, and shows the gate fail each time).

## What a number here means

A replay of hand-written answers. It proves the wiring and the checks, and that the gate fails when they break. It is
**not a model quality number**. One attempt (`"attempts": 1`); live runs with a key are manual check 2.

## Run it

    cd backend
    uv run --frozen python -m app.evals run record_proposals
    uv run --frozen python -m app.evals gate record_proposals --run <the id printed last>

Files: `build.py` (authored scripts; writes the next two), `gold.jsonl`, `recordings/` with `manifest.json`
(`{file: {task: prompt_version}}`), `score.py`, `baseline.json`. The world and its `_test` guard are `evals/chat/seed.py`.

## Corrections after a run

None.
