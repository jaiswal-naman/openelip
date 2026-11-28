# Testing

How OpenElip is tested, what each layer can and cannot show, which tests cover which feature, and what one run with a real model found.

**Read this first.** Almost every automated check here runs on invented data and recorded model answers. That proves the code does what it was written to do. It says nothing about how accurate any model is. No live gold-set run exists, so **no accuracy number for any model is claimed anywhere in this repository**. The one run with a real model ([below](#real-model-scenario-run)) checked that the whole flow works end to end and what it cost; it did not measure accuracy.

## The layers

| Layer | Where | What it tests | Needs |
|---|---|---|---|
| Backend unit and integration | `backend/tests/` (pytest) | Logic, SQL, row-level security, queue, gateway, HTTP routes. Most tests use a real PostgreSQL, so "unit" and "integration" sit side by side in each module's folder | A PostgreSQL with pgvector; a database whose name ends in `_test` |
| Static checks | `backend/` | `ruff check`, `ruff format --check`, `mypy app`, `lint-imports` (modules import only downward) | `uv` |
| Migration check | CI, `ci.yml` | An empty database upgrades to head, downgrades to the baseline, upgrades again | PostgreSQL |
| Web unit | `web/src/**/*.test.ts(x)` (Vitest, Testing Library, jsdom) | Components, hooks and label helpers, with the API faked | Node, pnpm |
| Web static checks | `web/` | `pnpm typecheck` (tsc), `pnpm lint` (eslint), and a check that `web/openapi.json` matches the backend and `schema.d.ts` matches `openapi.json` | Node, pnpm |
| Browser | `web/tests/e2e/` (Playwright, `@axe-core/playwright`) | Real user journeys against the running Compose stack, and an axe scan of each screen in the light and dark themes | A running stack, Chromium |
| Eval suites (replay) | `evals/`, run by `python -m app.evals` | **Wiring.** Recorded answers go through the real code path (task, validator, gateway, ledger, scorer), and a regression gate compares the score with a floor | PostgreSQL, `LLM_PROVIDER=fake` |
| Live accuracy runs (opt-in) | `python -m app.evals run <suite> --live` | **Measurement** of a real model on a gold set. Never run by a pull request or by the app. They gate nothing | A model key, which they spend |
| Secret scan | CI, gitleaks | No secret in any commit | none |

The pull-request gates are `ci.yml` (backend, eval gate per suite, web, secret scan) and `e2e.yml` (browser tests, on pull requests, on pushes to `main` and nightly). `images.yml` builds both images on pull requests that touch the code, without pushing, so a broken Dockerfile shows before a merge.

### Test data and safety

- **Backend tests refuse any database whose name does not end in `_test`.** `tests/conftest.py` creates it through the bootstrap login if it is missing and migrates it to head, then points every connection at it as the real `app_api`, `app_worker` and `app_match` roles, so row-level security applies.
- **No test reaches a host outside the computer.** `conftest.py` blocks httpx's real transports and fails the test that tries (`tests/platform/test_no_network.py`).
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

### Browser tests run on three kinds of stack

The specs create accounts and write cost rows, so point them at a **throwaway** stack, never at one that holds your own data.

| Stack | How it starts | Specs |
|---|---|---|
| Standard stack in server mode with the fake provider | `cp .env.example .env`, then `APP_MODE=server LLM_PROVIDER=fake docker compose up -d --build --wait` | `a11y.spec.ts`, `phase1.spec.ts`, `jobs-search.spec.ts`, `jobs-detail.spec.ts`, the signed-out and axe groups of `career-record.spec.ts` |
| Local-mode stack, exactly as `.env.example` has it | `cp .env.example .env`, `docker compose up -d --build --wait`, then run the spec with `LOCAL_STACK=1` | `local-mode.spec.ts` |
| Demo stack, one demo per fresh stack | `docker compose -p openelip-demo -f docker-compose.yml -f docker-compose.demo.yml up -d --build --wait` (a database named `app_demo_test`, the fake provider, invented company pages, ports 8090 and 8026), then `DEMO_STACK=1` | `matches-brief.spec.ts`, `chat-tailor-apply.spec.ts`, `hiring-desk.spec.ts`, `campaigns.spec.ts` |

The fake provider hands each recorded answer out once per process, so each demo spec needs a fresh demo stack (`down -v`, then `up`); `e2e.yml` does exactly that between specs. The full journey inside `career-record.spec.ts` (agree, upload, check facts, edit, export, delete) also needs `DEMO_STACK=1` on a throwaway stack and **is not part of the demo job in `e2e.yml`**; run it by hand.

**Status of the browser specs.** They were written before the interface redesign of 2026-11-28 and are wired into `e2e.yml`. This document does not state that they currently pass against the redesigned screens; the first run of that workflow on a pull request is the check.

### Eval suites

Run from `backend/`, with `DATABASE_URL_WORKER` pointing at a migrated `_test` database (CI uses one named `evals_test`) and `LLM_PROVIDER=fake`:

```bash
uv run python -m app.evals run <suite>              # replays evals/recorded/<suite>.json, prints a run id on its last line
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
| `dossier` | The Researcher keeps only claims whose quote is an exact substring of the fetched page | 30 invented companies |
| `agent_drafts` | The Writer, Chaser and Coach drafts keep only sentences their cited sources support | 50 invented items |
| `guardian` | Outside-action cases through the policy engine and the checks | 60 invented cases |
| `location` (own scorer) | The location parser reads country and city, and leaves unknown what a posting does not say | 400-row sample, 260 scored |
| `matching_benchmark` | Keyword, vector, hybrid and hybrid-plus-rules ranking compared on quality, cost and latency | 758 public postings, 20 invented profiles, 366 labelled pairs |
| wind tunnel | Whole campaigns over simulated days against scripted people, with six invariants checked. Two invented scenarios | `evals/wind_tunnel/` |

The thirteen suites from `harness_smoke` to `guardian` are the matrix of the eval gate in `ci.yml`, replayed on every pull request. `location`, `matching_benchmark` and the wind tunnel are run by pytest instead (`tests/evals/test_location_suite.py`, `test_matching_benchmark.py`, `tests/evals/windtunnel/`). **A pass means the code path works.** The recordings are built from the labels, so the score cannot show model quality.

### Live accuracy runs (opt-in)

```bash
docker compose exec worker python -m app.evals run resume_extraction --live
docker compose exec worker python -m app.evals run requirement_extraction --live
docker compose exec worker python -m app.evals run role_intake --live
docker compose exec worker python -m app.evals run evidence_faithfulness --live
```

A live run calls your configured model through the app's own gateway and the task's own code path, so every request is a row in the cost ledger. It **measures and never gates**: a run that fails closes no feature and a run that passes opens none. Only a person types the command. [evals/README.md](../evals/README.md) lists the bars each suite pre-registered and estimates the cost of one full check (an estimate at one provider's prices, not a measurement). **No live run has been made**, so there is no accuracy number to quote.

## Feature coverage

What exists today, by feature. The rows are the surfaces listed in `PRODUCT.md`. Paths are relative to `backend/tests/` for backend, `web/src/` for Vitest, `web/tests/e2e/` for Playwright and `evals/` for the suites. "none" means no test of that kind exists, not that a test is pending.

| Feature | Unit | Integration (backend, `_test` database) | Browser (Playwright) | Eval suites (replay) | Real-model scenario |
|---|---|---|---|---|---|
| **Today** | `features/matches/TodayView`, `DecisionCard`, `day` | `match/test_routes_matches.py`, `test_api_reads.py`, `test_summary.py` | `matches-brief.spec.ts` | none | The match run behind Today: 5 strong fits |
| **Matches and the "Why not" view** | `features/matches/` (`EvidenceTable`, `HeldDecision`, `MatchDetail`, `ConstraintChecks`, `DismissDialog`, `TextPane`); backend `match/test_checker.py`, `test_validator.py`, `test_ranking.py` | `match/test_run.py`, `test_run_invariants.py`, `test_constraint_property.py`, `test_evidence.py`, `test_retrieval.py`, `test_routes_decisions.py`, `test_feedback.py`, `test_material_change.py`, `test_schedule.py`; `db/test_match_schema.py` | `matches-brief.spec.ts` (read the evidence, see why not, both themes) | `constraint_property`, `evidence_faithfulness`, `matching_benchmark` | 5 strong fits, 8 withheld, 2 removed by the person's hard rules; top match cited 8 of 8 requirements |
| **Jobs search and the job index** | `features/jobs/` (`JobsView`, `JobCard`, `JobDetail`, `RequirementList`, `LoadMore`); backend `ingest/requirements/test_rules.py`, `test_segment.py`, `test_validate.py`, `ingest/test_location.py` | `ingest/test_search.py`, `test_search_constrained.py`, `test_embedding.py`, `test_index.py`, `test_store.py`, `test_liveness.py`, `test_fetch_schedule.py`, `test_import.py`, `test_extract_sweep.py`, `test_routes_jobs.py`; `db/test_job_index_schema.py` | `jobs-search.spec.ts`, `jobs-detail.spec.ts`, `a11y.spec.ts` | `location`, `requirement_extraction` | Requirements extracted for 16 of 16 postings; the duplicate posting folded. Search by meaning was not part of the run |
| **Career record** | `features/record/*`, `features/onboarding/*`, `features/consent/AiConsentSheet`; backend `profile/test_extract_validate.py`, `test_reader.py`, `test_facts_usable.py`, `test_contact.py`, `test_protected.py` | `profile/test_upload_parse.py`, `test_extract_workflow.py`, `test_documents_routes.py`, `test_routes_record.py`, `test_record_service.py`, `test_change_log.py`, `test_intake.py`, `test_ai_gate.py`; `trust/test_consent.py` | `career-record.spec.ts` (the full journey needs the demo stack, see above) | `resume_extraction`, `record_proposals` | Resume read into 46 facts, each quoting its source line |
| **Tailored resumes** | `features/tailor/TailorView`, `lib/wordDiff`; backend `tailor/test_verifier.py`, `test_resume.py`, `test_export.py` | `tailor/test_pipeline.py`, `test_service.py`, `test_routes.py`; `db/test_tailor_apply_schema.py` | `chat-tailor-apply.spec.ts` | `tailoring_integrity` | Tailored resume ready; DOCX exported. Found a PDF export problem (see Real-model scenario run) |
| **Applications** (tracker and packets) | `features/applications/ApplicationsSection` | `apply/test_applications.py`, `test_packets.py`, `test_campaign_packet.py` | `chat-tailor-apply.spec.ts` (download the packet, record applying) | `agent_drafts` (Writer) | Application packet ready |
| **Campaigns** (board, timeline, packet, dossier, crew, prep, follow-ups) | `features/campaigns/` (`Board`, `CampaignView`, `StartCampaignButton`), `features/chat/CampaignCard`, `lib/campaignLabels`; backend `campaigns/crew/test_fit.py`, `test_research.py`, `test_chaser.py`, `test_coach.py` | `campaigns/test_graph_flow.py`, `test_kill_resume.py`, `test_nodes.py`, `test_research_loop.py`, `test_routes_campaigns.py`, `test_views.py`, `test_board.py`, `test_start_paths.py`; `db/test_campaign_schema.py`, `test_langgraph_schema.py` | `campaigns.spec.ts` (demo stack) | `dossier`, `agent_drafts`, `guardian`, wind tunnel | A campaign crew reached its approval step |
| **Approvals and undo** | `features/campaigns/Approvals`; backend `guardian/test_policy.py`, `test_checks.py`, `test_explain.py` | `guardian/test_decide_release.py`, `test_undo.py`, `test_expire.py`, `test_settle.py`, `test_sender.py`, `test_routes_approvals.py`, `test_invariant_5.py`, `test_invariant_6.py` (nothing is sent twice); `db/test_two_key_schema.py` | `campaigns.spec.ts` (approve and undo a follow-up; a blocked one shows its reason) | `guardian` | The campaign was approved at its approval step. Undo and follow-ups were not exercised |
| **Autonomy settings** | `features/autonomy/AutonomySection`; backend `guardian/test_autonomy.py` | `guardian/test_routes_autonomy.py`, `test_budget.py` | `campaigns.spec.ts` (set follow-ups to Never; the locked row shows why) | `guardian` | not run |
| **Flight recorder** | `features/recorder/RecorderView` | `guardian/test_recorder.py`, `test_routes_recorder.py`, `test_stream_source.py`, `campaigns/test_control_runs.py` | `campaigns.spec.ts` (step through the recorder) | none | not exercised |
| **Costs** | `features/control/CostsView`; backend `platform/llm/test_costs.py` | `platform/test_control_costs_month.py`, `campaigns/test_control_costs.py`, `platform/test_gateway_attribution.py`, `platform/llm/test_gateway.py` | `phase1.spec.ts` (a model cost row), `a11y.spec.ts` | none | The run's totals: 57 calls, USD 0.106 |
| **Eval scoreboard** | none: there is no screen | `evals/test_scoreboard_routes.py`, `evals/test_gate.py`, `evals/windtunnel/` | none | the suites above | No live gold-set run exists |
| **Hiring desk** (roles, shortlist, answers, team) | `features/hiring/` (`RoleIntake`, `PreviewPanel`, `Shortlist`, `AnswerClock`, `AnswerDialog`), `features/introductions/*`; backend `hiring/test_preview_builder.py`, `test_intake.py`, `test_access.py` | `hiring/test_publish.py`, `test_asks.py`, `test_accept.py`, `test_answers.py`, `test_answer_clock.py`, `test_shortlist.py`, `test_org_routes.py`, `test_role_routes.py`, `test_introduction_routes.py`, `test_invariant_4.py` (nothing about a candidate reaches an employer without their action), `test_blocked_employer.py`; `db/test_hiring_schema.py`, `test_hiring_rls.py` | `hiring-desk.spec.ts` (demo stack) | `role_intake` | not run |
| **Trust** (what is sent where) | `features/trust/TrustView` | `trust/test_subprocessors.py`, `test_consent.py`, `test_audit.py` | `a11y.spec.ts` (`/trust`) | none | not run |
| **Chat agent** | `features/chat/` (`AgentPage`, `ChatPanel`, `MessageText`, `reply`, `transcript`, `ChatOpener`); backend `chat/test_refs.py`, `test_proposals.py`, `test_tools.py` | `chat/test_loop.py`, `test_service.py`, `test_routes.py`, `test_stream.py`, `test_campaign_tools.py`; `db/test_chat_schema.py` | `chat-tailor-apply.spec.ts` | `chat`, `record_proposals`, `injection` | Two streamed answers, with citations |

### Cross-cutting

| Area | Tests |
|---|---|
| Setup and local mode (no sign-in, model key stored encrypted, localhost-only) | `features/local/*`; `platform/test_local_mode.py`; `local-mode.spec.ts` |
| Model gateway, queue, scheduler, outbox, idempotency, sign-in | `platform/llm/*`, `platform/test_queue.py`, `test_scheduler.py`, `test_relay.py`, `test_idempotency.py`, `test_signin_flow.py`, `test_outbox_actions.py`; `phase1.spec.ts` |
| Morning brief | `features/brief/*`; `brief/test_compose.py`, `test_mail.py`, `test_sweep.py`, `test_unsubscribe.py`, `test_routes_briefs.py`; `matches-brief.spec.ts`. Not exercised in the real-model run |
| Export and deletion of an account | `features/privacy/PrivacySection`; `trust/test_export.py`, `test_erase_function.py`, `test_deletion_flow.py`, `test_deletion_catalogue.py`, `test_registry_contract.py`; the journey in `career-record.spec.ts` |
| Privacy of logs and the network | `platform/test_content_free_canary.py`, `test_log.py`, `test_no_tracing.py`, `test_no_network.py` |
| Schema, migrations and row-level security | `db/` (a file per area, including `test_hiring_rls.py`), the migration check in `ci.yml` |
| Accessibility | `a11y.spec.ts` scans each public and signed-in screen with axe in both themes; the demo specs scan their own screens too. Axe finds only part of what WCAG asks for |
| Documents that tests read | A few schema and eval tests parse `docs/DATA_MODEL.md`, `docs/AI_SYSTEM.md` and each suite's README, so a documentation edit can fail a test |

**Not built, so only partly tested.** The Twin (answers to employer questions from the record) has backend logic and a schema (`twin/test_intake.py`, `test_logic.py`, `test_schemas.py`, `db/test_twin_schema.py`) but no routes and no screen. The MCP interface for outside agents has a token schema (`db/test_agent_token_schema.py`) and no routes. Neither has a browser or real-model test.

## Real-model scenario run

**What it was.** On 2026-11-27 a fresh install was driven end to end with a real OpenAI key (the OpenAI routes in [`backend/config/models.toml`](../backend/config/models.toml)), one real resume (not published, not in this repository) and 16 invented job postings, among them a duplicate and postings that break the person's stated hard rules. The aim was to see the whole flow work on a real model and to find what breaks. It was one run on one resume. It is not a measurement of accuracy and not a benchmark.

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
| Campaign | A campaign crew reached its approval step and was approved |
| Chat | Two streamed answers, each with citations |

**What the run found.**

- **PDF export could not draw common punctuation.** The tailored resume's PDF export failed on punctuation a resume carries. This is being fixed; the DOCX export was not affected.
- **Four mistakes in the test script itself.** The script that drove the run was wrong in four places; they were corrected in the script. They were not faults of the product.

**What it does not show.**

- **No accuracy number exists.** There has been no live gold-set run, so nothing here says how accurate the model's resume reading, requirement extraction or match verdicts are. "46 facts each quoting its line" shows that every fact is anchored to a source line, not that the 46 are the right facts. "5 strong fits" is what this model returned for postings written to fit, not a precision figure.
- It used one provider. The Anthropic adapter and the local-model route have no recorded live run.
- It did not exercise the hiring desk, the morning-brief email, autonomy settings, follow-up emails and undo, or the flight recorder.
- It used 16 invented postings, not the live job boards.
- No quality claim may be made from it (invariant 8 in [OVERVIEW.md](OVERVIEW.md)).

## Known gaps

- No live gold-set run for any model (above).
- The gold sets' labels are drafted by an agent; the maintainer has not reviewed them. Several sets are smaller than the sizes the design asks for; each README states its size and target.
- No search-quality gold set.
- The browser specs have not been re-run against the redesigned screens (see How to run each), and two journeys need a demo stack that `e2e.yml` does not start for them.
- No Twin or MCP routes, and no screen for the evaluation scoreboard.
- No load or performance test beyond the measurements quoted in the README.

### Agent stages on the real model (28 November 2026)

The same install and key, with four more scripted stages. Totals across both days: 131 model calls, 0 failures, USD 0.24.

| Agent | What happened |
|---|---|
| Chat record proposals (`record.propose`) | A chat message ("only remote roles, nothing under 6 lakh a year, I know Kubernetes") became proposals to confirm; the record was unchanged until confirmed |
| Researcher | A live public Greenhouse board was fetched; the Researcher read the company's pages and kept quoted claims. The planner used its whole turn budget when fetches failed (a known inefficiency) |
| Guardian | An approved packet was released. Before the fix in this release, the model judge had passed it at proposal and failed the same unchanged packet at approval; a citation verdict that passed on identical content is now reused |
| Chaser | After eight quiet days in `applied`, it drafted a follow-up email with the model; it waits for approval |
| Coach | It drafted four interview questions; verification removed all four (`framing_not_entailed` 3, `framing_new_entity` 1), so the prep tab was empty. Known issue, open |
