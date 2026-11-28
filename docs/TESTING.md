# Testing

How OpenElip is tested, what each layer can and cannot show, which tests cover which feature, and what one run with a real model found. This page describes what is on `main`.

**Read this first.** Almost every automated check here runs on invented data and recorded model answers. That proves the code does what it was written to do. It says nothing about how accurate any model is. No live gold-set run exists, so **no accuracy number for any model is claimed anywhere in this repository**. The one run with a real model ([below](#real-model-run-on-a-development-build)) was made on a development build that is not on `main`, checked that the flow works end to end and what it cost, and did not measure accuracy.

## The size of it

Counted on `main`:

| What | Count |
|---|---|
| Backend test files (`backend/tests/`) | 167 |
| Backend test functions (every `def test_`; parametrised tests run more cases than this) | 2,575 |
| Web test files (Vitest, under `web/src/`) | 67 |
| Playwright specs (`web/tests/e2e/*.spec.ts`) | 8 |
| Eval suites replayed and gated by the CI matrix | 10 (`harness_smoke`, `resume_extraction`, `requirement_extraction`, `evidence_faithfulness`, `constraint_property`, `tailoring_integrity`, `chat`, `record_proposals`, `injection`, `role_intake`) |
| Further eval suites run by pytest | 2 (`location`, `matching_benchmark`) |

## The layers

| Layer | Where | What it tests | Needs |
|---|---|---|---|
| Backend unit and integration | `backend/tests/` (pytest) | Logic, SQL, row-level security, queue, gateway, HTTP routes. Most tests use a real PostgreSQL, so "unit" and "integration" sit side by side in each module's folder | A PostgreSQL with pgvector; a database whose name ends in `_test` |
| Static checks | `backend/` | `ruff check`, `ruff format --check`, `mypy app`, `lint-imports` (modules import only downward) | `uv` |
| Migration check | CI, `ci.yml` | An empty database upgrades to head, downgrades to the baseline, upgrades again | PostgreSQL |
| Web unit | `web/src/**/*.test.ts(x)` (Vitest, Testing Library, jsdom) | Components, hooks and label helpers, with the API faked | Node, pnpm |
| Web static checks | `web/` | `pnpm typecheck` (tsc), `pnpm lint` (eslint), and a check that `web/openapi.json` matches the backend and `schema.d.ts` matches `openapi.json` | Node, pnpm |
| Browser | `web/tests/e2e/` (Playwright, `@axe-core/playwright`) | Real user journeys against the running Compose stack, and axe accessibility scans of the main screens in the light and dark themes | A running stack, Chromium |
| Eval suites (replay) | `evals/`, run by `python -m app.evals` | **Wiring.** Recorded answers go through the real code path (task, validator, gateway, ledger, scorer), and a regression gate compares the score with a floor | PostgreSQL, `LLM_PROVIDER=fake` |
| Live accuracy run (opt-in) | `python -m app.evals run evidence_faithfulness --live` | **Measurement** of a real model on one gold set. Never run by a pull request or by the app | A model key, which it spends |
| Secret scan | CI, gitleaks | No secret in any commit | none |

The pull-request gates are `ci.yml` (backend, eval gate per suite, web, secret scan) and `e2e.yml` (browser tests, on pull requests, on pushes to `main` and nightly).

### Test data and safety

- **Backend tests refuse any database whose name does not end in `_test`.** `tests/conftest.py` points every connection at it as the real `app_api`, `app_worker` and `app_match` roles, so row-level security applies.
- **Everything is invented.** The people, companies, postings and resumes in tests, gold sets and demo seeds are made up and labelled so. Gold-set labels were drafted by an agent and are awaiting maintainer review; each suite's README says so, and a result is always reported with those words.
- **No text leaks into logs.** `tests/platform/test_content_free_canary.py` seeds unique canary strings (an email, a resume paragraph, a chat message, a search query, an exception message) and fails if any appears in a log line, a metric or a trace.

## How to run each

```bash
# Backend (needs the database: docker compose up -d db). The default test database is app_test on 127.0.0.1:5433;
# set TEST_DATABASE_URL to use another one whose name ends in _test.
cd backend
uv sync --frozen
uv run pytest tests -q                              # everything
uv run pytest tests/match -q                        # one module
uv run ruff check app tests ../db && uv run ruff format --check app tests ../db
uv run mypy app && uv run lint-imports

# Web
cd web
pnpm install
pnpm typecheck && pnpm lint && pnpm test --run

# Browser (a stack must be running; see below)
cd web
pnpm exec playwright install chromium
pnpm exec playwright test tests/e2e --project=chromium

# Every gate of ci.yml on Windows, one PASS or FAIL line each
powershell -File scripts/ci-local.ps1
```

### Browser tests run on two kinds of stack

The specs create accounts and write cost rows, so point them at a **throwaway** stack, never at one that holds your own data.

| Stack | How it starts | Specs |
|---|---|---|
| Standard stack | `cp .env.example .env`, then `docker compose up -d --build --wait` | `a11y.spec.ts`, `phase1.spec.ts`, `jobs-search.spec.ts`, `jobs-detail.spec.ts`, the signed-out and axe groups of `career-record.spec.ts` |
| Demo stack, one demo per fresh stack | `docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait` (a database named `app_demo_test`, the fake provider, ports 8090 and 8026), then `DEMO_STACK=1` and the two addresses | `matches-brief.spec.ts`, `chat-tailor-apply.spec.ts`, `hiring-desk.spec.ts` |

The fake provider hands each recorded answer out once per process, so each demo spec needs a fresh demo stack (`down -v`, then `up`); `e2e.yml` does exactly that between specs. The full journey inside `career-record.spec.ts` (agree, upload, check facts, edit, export, delete) also needs `DEMO_STACK=1` on a throwaway stack and is not part of the demo job in `e2e.yml`; run it by hand.

### Eval suites

Run from `backend/`, with `DATABASE_URL_WORKER` pointing at a migrated `_test` database (CI uses one named `evals_test`) and `LLM_PROVIDER=fake`:

```bash
uv run python -m app.evals run <suite>              # replays the suite's recording, prints a run id on its last line
uv run python -m app.evals gate <suite> --run <id>  # exit 0 if the run meets its floor in evals/thresholds.toml, 1 if not
```

`requirement_extraction` is scored by span overlap by its own script: `uv run python ../evals/requirement_extraction/score.py --record`. See [evals/README.md](../evals/README.md) for how suites are added and gated.

| Suite | What it checks | Size |
|---|---|---|
| `harness_smoke` | That the runner, the recorded rows and the gate work. A deliberately degraded recording must fail the gate | 5 invented messages |
| `resume_extraction` | Resume facts and their quoted source lines | 120 invented resumes |
| `requirement_extraction` | A posting's requirements, required or preferred, with their exact spans | 50 real postings (labels drafted by an agent) |
| `evidence_faithfulness` | Each requirement gets the verdict the record supports, citing facts that exist | 30 resume and posting pairs |
| `constraint_property` | The SQL hard-constraint filter and the independently written checker agree on every case. Needs no model and no labels | 14,400 generated job and constraint-set pairs |
| `tailoring_integrity` | No resume edit that adds something the confirmed facts do not contain gets through both checks | 30 pairs |
| `chat` | The chat agent's turn loop with its real tools on the real database | 20 invented scripts |
| `record_proposals` | A proposed record change is held to the checks in code | 20 invented scripts |
| `injection` | Hidden instructions in a posting, resume fact or requirement are contained by structure, with a scripted hijacked model | 40 invented cases |
| `role_intake` | An employer's role description is read into requirement items | 30 invented role descriptions |
| `location` (own scorer, run by pytest) | The location parser reads country and city, and leaves unknown what a posting does not say | 400-row sample, 260 scored |
| `matching_benchmark` (run by pytest) | Keyword, vector, hybrid and hybrid-plus-rules ranking compared on quality, cost and latency | 758 public postings, 20 invented profiles, 366 labelled pairs |

The ten suites from `harness_smoke` to `role_intake` are the matrix of the eval gate in `ci.yml`, replayed on every pull request. `location` and `matching_benchmark` are run by pytest instead (`tests/evals/test_location_suite.py`, `test_matching_benchmark.py`). **A pass means the code path works.** The recordings are built from the labels, so the score cannot show model quality.

### Live accuracy run (opt-in)

```bash
docker compose exec worker python -m app.evals run evidence_faithfulness --live
```

A live run calls your configured model through the app's own gateway and the task's own code path, so every request is a row in the cost ledger. It is the only suite with a live mode on `main`. A passed run of it is what opens match scoring for that model and prompt; the same kind of gate guards resume reading, requirement extraction and role intake, and those three have no live command yet, so they stay off with a real model on `main` (see [the README](../README.md#-core-features)). [evals/README.md](../evals/README.md) and each suite's README list the bars it pre-registered. **No live run has been made**, so there is no accuracy number to quote.

## Feature coverage

What exists today, by feature. Paths are relative to `backend/tests/` for backend, `web/src/` for Vitest, `web/tests/e2e/` for Playwright and `evals/` for the suites. "none" means no test of that kind exists, not that a test is pending.

| Feature | Unit | Integration (backend, `_test` database) | Browser (Playwright) | Eval suites (replay) |
|---|---|---|---|---|
| **Today and the morning brief** | `features/matches/`, `features/brief/` | `brief/` (`test_compose.py`, `test_mail.py`, `test_sweep.py`, `test_unsubscribe.py`, `test_routes_briefs.py`), `match/test_summary.py`; `db/test_brief_schema.py` | `matches-brief.spec.ts` (demo stack) | none |
| **Matches and the "Why not" view** | `features/matches/`; backend `match/test_checker.py`, `test_validator.py`, `test_ranking.py` | `match/test_run.py`, `test_run_invariants.py`, `test_constraint_property.py`, `test_evidence.py`, `test_retrieval.py`, `test_routes_decisions.py`, `test_routes_matches.py`, `test_feedback.py`, `test_material_change.py`, `test_ai_gate.py`; `db/test_match_schema.py` | `matches-brief.spec.ts` (read the evidence, see why not, both themes) | `constraint_property`, `evidence_faithfulness`, `matching_benchmark` |
| **Jobs search and the job index** | `features/jobs/`; backend `ingest/requirements/` (`test_rules.py`, `test_segment.py`, `test_validate.py`), `ingest/test_location.py` | `ingest/test_search.py`, `test_search_constrained.py`, `test_embedding.py`, `test_index.py`, `test_store.py`, `test_liveness.py`, `test_fetch_schedule.py`, `test_import.py`, `test_extract_sweep.py`, `test_routes_jobs.py`; `db/test_job_index_schema.py` | `jobs-search.spec.ts`, `jobs-detail.spec.ts`, `a11y.spec.ts` | `location`, `requirement_extraction` |
| **Career record** | `features/record/`, `features/onboarding/`, `features/consent/`; backend `profile/test_extract_validate.py`, `test_reader.py`, `test_facts_usable.py`, `test_contact.py`, `test_protected.py` | `profile/test_upload_parse.py`, `test_extract_workflow.py`, `test_documents_routes.py`, `test_routes_record.py`, `test_record_service.py`, `test_change_log.py`, `test_intake.py`, `test_ai_gate.py`; `trust/test_consent.py` | `career-record.spec.ts` (the full journey needs the demo stack, see above) | `resume_extraction`, `record_proposals` |
| **Chat agent** | `features/chat/`; backend `chat/test_refs.py`, `test_proposals.py`, `test_tools.py` | `chat/test_loop.py`, `test_service.py`, `test_routes.py`, `test_stream.py`; `db/test_chat_schema.py` | `chat-tailor-apply.spec.ts` (demo stack) | `chat`, `record_proposals`, `injection` |
| **Tailored resumes** | `features/tailor/`; backend `tailor/test_verifier.py`, `test_resume.py`, `test_export.py` | `tailor/test_pipeline.py`, `test_service.py`, `test_routes.py`; `db/test_tailor_apply_schema.py` | `chat-tailor-apply.spec.ts` | `tailoring_integrity` |
| **Application packets and the tracker** | `features/applications/` | `apply/test_applications.py`, `test_packets.py` | `chat-tailor-apply.spec.ts` (download the packet, record applying) | none |
| **Hiring desk** (roles, shortlist, answers, team) | `features/hiring/`, `features/introductions/` | `hiring/` (`test_publish.py`, `test_asks.py`, `test_accept.py`, `test_answers.py`, `test_answer_clock.py`, `test_shortlist.py`, `test_org_routes.py`, `test_role_routes.py`, `test_introduction_routes.py`, `test_invariant_4.py` (nothing about a candidate reaches an employer without their action), `test_blocked_employer.py`, `test_signals.py`); `db/test_hiring_schema.py`, `test_hiring_rls.py` | `hiring-desk.spec.ts` (demo stack) | `role_intake` |
| **Costs** | `features/control/` | `platform/llm/test_costs.py`, `test_gateway.py` | `phase1.spec.ts` (a model cost row), `a11y.spec.ts` | none |
| **Trust** (what is sent where) | `features/trust/` | `trust/test_subprocessors.py`, `test_consent.py`, `test_audit.py` | `a11y.spec.ts` (`/trust`) | none |

### Cross-cutting

| Area | Tests |
|---|---|
| Model gateway, queue, scheduler, outbox, idempotency, sign-in | `platform/llm/` (`test_gateway.py`, `test_routing.py`, `test_adapters.py`, `test_costs.py`), `platform/test_queue.py`, `test_scheduler.py`, `test_relay.py`, `test_idempotency.py`, `test_signin_flow.py`; `phase1.spec.ts` |
| Export and deletion of an account | `features/privacy/`; `trust/test_export.py`, `test_erase_function.py`, `test_deletion_flow.py`, `test_deletion_catalogue.py`, `test_registry_contract.py`; the journey in `career-record.spec.ts` |
| Privacy of logs | `platform/test_content_free_canary.py`, `test_log.py`, `test_no_tracing.py` |
| Schema, migrations and row-level security | `db/` (a file per area, including `test_hiring_rls.py`), the migration check in `ci.yml` |
| Accessibility | `a11y.spec.ts` scans `/`, `/jobs`, `/sign-in`, `/trust` and `/control/costs` with axe in both themes; the demo specs scan their own screens too. Axe finds only part of what WCAG asks for |
| Documents that tests read | A few schema and eval tests parse `docs/DATA_MODEL.md`, `docs/AI_SYSTEM.md` and each suite's README, so a documentation edit can fail a test |

**Not built, so not tested.** Campaigns, the Guardian, the Twin and the MCP interface are not on `main`.

## Real-model run on a development build

**What it was.** On 28 November 2026 a fresh install of a development build was driven end to end with a real OpenAI key, one real resume (not published, not in this repository) and 16 invented job postings, among them a duplicate and postings that break the person's stated hard rules. The aim was to see the whole flow work on a real model and to find what breaks. It was one run on one machine with one resume. It is not a measurement of accuracy and not a benchmark.

**That build is not `main`.** It had OpenAI routes for every task, which `main` does not (on `main` only the `ping` check has an OpenAI route; see [the README](../README.md#-model-providers)), and it had no live-run gate on resume reading, requirement extraction or match scoring. The run therefore cannot be repeated on `main` with an OpenAI key. It is reported here because most of the code it exercised is on `main`.

**Measured results.**

| Step | Result |
|---|---|
| Model calls | 57 made, 0 failed, USD 0.106 in total |
| Resume reading | The resume became 46 facts, each quoting the resume line it came from |
| Requirement extraction | 16 of 16 postings had their requirements extracted |
| Duplicates | The duplicate posting was folded onto one job |
| First match run | 5 strong fits shown; 8 withheld; 2 removed by the person's hard rules (a US onsite job, a low-pay job). The top match cited 8 of its 8 requirements |
| Tailoring | A tailored resume was ready, and the DOCX export worked |
| Application packet | Ready |
| Chat | Two streamed answers, each with citations |
| Campaign | A campaign reached its approval step and was approved. Campaigns are not on `main` |

**What the run found.**

- **PDF export could not draw common punctuation.** The tailored resume's PDF export failed on punctuation a resume carries, such as en dashes and curly quotes: the PDF uses a built-in Latin-1 font and refuses a character above U+00FF. This is on `main` too and is listed under [known issues](../README.md#-known-issues). A fix is made for the next release. The DOCX export was not affected.
- **The Guardian re-ran its model citation judge after approval** and could block an approved action when the verdict flipped. The Guardian is part of campaigns and is not on `main`; the fix is part of the next release.

**What it does not show.**

- **No accuracy number exists.** There has been no live gold-set run, so nothing here says how accurate the model's resume reading, requirement extraction or match verdicts are. "46 facts each quoting its line" shows that every fact is anchored to a source line, not that the 46 are the right facts. "5 strong fits" is what this model returned for postings written to fit, not a precision figure.
- It used one provider. The Anthropic adapter and the local-model route have no recorded live run.
- It did not exercise the hiring desk or the morning-brief email.
- A second run, covering the Chaser, the Coach, chat record proposals and the Researcher reading live pages, **has not yet been run on a real model.** Neither has anything else not listed above.
- It used 16 invented postings, not the live job boards.
- No quality claim may be made from it (invariant 8 in [OVERVIEW.md](OVERVIEW.md)).

## Known gaps

- No live gold-set run for any model (above).
- The gold sets' labels are drafted by an agent; the maintainer has not reviewed them. Several sets are smaller than the sizes the design asks for; each README states its size and target.
- No search-quality gold set.
- No Twin or MCP routes, and no screen for the evaluation scoreboard.
- No load or performance test beyond the measurements quoted in the README.
