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
same every time. A live run (a real model, a real key) is a maintainer's measurement of a model; `run <suite> --live`
makes it for a suite whose scorer declares one (today `resume_extraction`, `requirement_extraction`, `role_intake` and
`evidence_faithfulness`: see "Live runs" below) and it is never run by pull requests or by the application itself.
`--recorded FILE` replays another recording of the same suite.

The gate reads only what a run recorded. It fails, rather than passes, when the suite has no floor, the run does not
exist, did not finish, ended in an error, or belongs to another suite.

## Live runs: measuring a model

Features run on any model that is configured. A gold-set run does not open a feature when it passes and does not close
one when it fails: it measures how accurate a model is on a task, and the numbers are published for that model once a
run exists (AI_SYSTEM 2.2 and 12.10). **No live run exists yet**, so no accuracy number for any real model is claimed
anywhere in this repository. Four places (the posting sweep, the resume upload, the match run and role intake) write one
info line, `unproven`, when a real model has no passed run, so an operator can see what nobody has measured; nothing else
changes.

Nothing runs a live run by itself. It spends the key of whoever starts it, so it is a command somebody types:

```
# On a local install (the worker reads the key from .env, or the one saved in the app):
docker compose exec worker python -m app.evals run resume_extraction --live
docker compose exec worker python -m app.evals run requirement_extraction --live
docker compose exec worker python -m app.evals run role_intake --live
docker compose exec worker python -m app.evals run evidence_faithfulness --live

# On a checkout, from backend/, with LLM_PROVIDER, that provider's key and the application's DATABASE_URL_WORKER set:
uv run python -m app.evals run <suite> --live
uv run python -m app.evals gate <suite> --run <the id printed last>
```

Each run calls the real model through the application's own gateway and the task's own code path, so every request is a
row of the cost ledger (`llm_calls`). It is charged to no person, as the posting sweep's calls are, so the Costs page,
which lists a signed-in person's own spend, does not show it: read it from the ledger itself (`SELECT task, count(*),
sum(cost_microusd) FROM llm_calls GROUP BY task` as the database owner). It records `eval_runs.config` with `mode` `live`,
`method` `model`, the `provider`, the `model` the route names, the `prompt_version` (resume: `prompt_versions`) and, where
the suite has a split, `split`. The run is `passed` only when every bar its README pre-registered holds, `failed` when
one does not, and `error` when the provider stops answering (run it again). A run is frozen for good in the database it
was written to. Report a result with its counts and Wilson intervals and the words "labels drafted by an agent, review
pending" (or "invented resumes"), as each README says.

| Suite | Items | Requests | Bars (from the suite's README; none is new) |
|---|---|---|---|
| `resume_extraction` | 120 invented resumes, all of them the test split (no model has answered on them yet) | 360 (three tasks each) | field F1 overall at least 0.90; employer, title and dates F1 at least 0.95 (AI_SYSTEM 4.2); every kept quote equals its span (enforced: a mismatch ends the run in `error`) |
| `requirement_extraction` | 39 test postings (40 less `req-049`) | 39 (two for a posting no item of which survives validation) | `req_spans_resolved` = 1.0, `req_item_f1` at least 0.85, `req_importance_accuracy` at least 0.90 (AI_SYSTEM 4.3) |
| `role_intake` | 30 invented role descriptions | 30 (the same retry) | the same three bars as postings, the targets of AI_SYSTEM 4.3 |
| `evidence_faithfulness` | 30 resume and posting pairs | 30 to 60 | faithful verdicts at least 0.90, false `met` on required items at most 0.05, citation rejections at most 0.02 (AI_SYSTEM 4.4) |

**What one full check costs (an estimate, not a measurement).** Tokens are characters divided by 4, over the requests the
tasks really build from the gold sets plus their prompts; the answers are sized from the recordings, which are built from
the labels. At the prices of `claude-haiku-5-5` in `backend/config/models.toml` (US$0.10 in and US$0.50 out per million
tokens, read on 2026-11-22): `resume_extraction` about 197,000 input and 109,000 output tokens, US$0.07;
`requirement_extraction` about 77,000 and 20,000, US$0.02; `role_intake` about 18,000 and 6,000, under US$0.01;
`evidence_faithfulness` about 41,000 and 5,000, under US$0.01. All four come to about US$0.10. With 30% more tokens (the
newer tokenizer, AI_SYSTEM 2.12) and every request asked twice it is about US$0.27, and if every answer filled its output
cap (thinking is billed as output) the ceiling is about US$3.3. Another provider's cost follows its own prices for the
route; a route without a price is refused by the gateway, so no run guesses one. When this was written `models.toml` had
an OpenAI route only for the `ping` task, so none of these four tasks had one there yet.

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
`resume_extraction/facts/` (see its README). A script that also sets `LIVE = True` takes `run(None)`, which calls the
real model, and may set `LIVE_CONFIG`: facts that only a live run records (a split, the bars it was held to).
