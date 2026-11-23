# Evaluation sets

Every AI feature in this project gets a gold set here: items with the answer a person decided is correct. A run sends
each item through the model gateway, compares the answer with the expected one, and records the result in the
`eval_runs` and `eval_items` tables. The regression gate then checks that run against a floor in `thresholds.toml`.

## Layout

| Path | What it holds |
|---|---|
| `gold/<suite>.jsonl` | One JSON object per line: `key` (stable id), `text` (the input), `expected` (the answer), optional `slice` |
| `recorded/<suite>.json` | The model's answers for that suite, in item order, in a form the fake provider replays |
| `fixtures/` | Deliberately bad recordings, used to prove the gate fails when it should |
| `thresholds.toml` | The floor each suite's score must reach |

## Commands

Run from `backend/`, with `DATABASE_URL_WORKER` pointing at a migrated database.

```
python -m app.evals run harness_smoke            # replays recorded/harness_smoke.json, prints the run id
python -m app.evals gate harness_smoke --run ID  # exit 0 if the run meets its floor, 1 if not
```

`run` replays: the gateway's fake provider answers from the recording, so no model key is needed and the result is the
same every time. A live run (a real model, a real key) is for when a prompt or a model changes; it is added with the
first real gold set and is never run by pull requests. `--recorded FILE` replays another recording of the same suite.

The gate reads only what a run recorded. It fails, rather than passes, when the suite has no floor, the run does not
exist, did not finish, ended in an error, or belongs to another suite.

## harness_smoke: a harness check, never a quality result

`harness_smoke` has five short, **invented** messages and a label for each. It exists only to prove that the runner,
the recorded rows and the gate work. It says nothing about the quality of any model, prompt or feature, and no page,
README or report may present its score as a result. Its runs carry `harness_check_only` in their metrics, and its
suite value in the database is `harness_smoke` so any list of results can leave it out.

`fixtures/harness_smoke_degraded.json` holds wrong answers for three of the five items. The tests run it and require
the gate to fail.

## Adding a suite

1. Add its value to the `eval_suite` type in `docs/DATA_MODEL.md` and the migration if it is not there yet.
2. Write `gold/<suite>.jsonl` (real items only with written consent; invented ones are labelled as invented).
3. Add an entry to `SUITES` in `backend/app/evals/runner.py`: the task name, the instruction, the answer schema.
4. Record the answers in `recorded/<suite>.json`, in item order. An answer that fails the schema is asked for again, so
   it uses two entries.
5. Set its floor in `thresholds.toml` before the first run.

A suite whose items need several gateway calls, or a score that is not an exact match, registers as
`OwnScorer("<script path under evals/>")` instead of a `Suite`. Its script defines `TASK`, `CONFIG`, `RECORDED` and
`run(recorded) -> Scored`, and brings its own gold set and recording format. The runner still refuses a real provider,
writes the run record and finishes it, and the gate still compares `metrics.score` with the floor. The one example is
`resume_extraction/facts/` (see its README).
