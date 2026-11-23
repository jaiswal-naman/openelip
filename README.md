# OpenElip

An open-source, self-hosted agent hiring marketplace that runs on your own machine.

> **Independent project.** OpenElip is independent. It is not affiliated with, endorsed by, or built from any code or assets of Elip.

## What it is

Most job tools hand you a list. OpenElip is being built to do the work behind the list and to show its evidence:

- **Real jobs only.** Postings come from employers' own public job boards, are checked on every refresh, and are closed when they leave the board.
- **Dealbreakers enforced in code.** A job that breaks a stated hard constraint is filtered by the database before any model sees it.
- **Reasons you can check.** Each match is a requirement-by-requirement table that points at the exact sentence in the job post and the exact line in your resume.
- **Done for you, decided by you.** A crew of agents researches the company, prepares the application and drafts follow-ups. Nothing leaves the system without your yes.
- **Local first.** One `docker compose up`. Search runs with no API key. Language-model features use a key you supply, or a local model through an OpenAI-compatible endpoint.

## Status

Phase 1 (the platform) and phase 2 (the searchable job index) are built. The table says exactly what runs today and what does not. Every figure from a labelled set comes with the words "labels drafted by an agent, review pending": the labels were written by an agent and the maintainer has not reviewed them yet.

| Area | State |
|---|---|
| One command starts the stack: database, API, worker, web app, one-origin proxy, local mail catcher | **Working.** A fresh clone was started and checked with `scripts/fresh-clone-check.ps1` on 22 November 2026 |
| Sign-in with a six-digit code sent by email, server-side session, sign-out | **Working** (in development the email lands in the local mail catcher) |
| Writes that are safe to repeat (idempotency keys), an outbox and an email relay that never sends a message twice | **Working** |
| Background jobs in PostgreSQL: unique keys, leases, retries, dead letters, a scheduler | **Working** |
| Model gateway: one entry point, schema-checked answers, one cost row per call, `503 ai_disabled` when no provider is set | **Working** with the built-in fake provider and recorded answers. The Anthropic, OpenAI and OpenAI-compatible adapters are written and tested against stub clients; **no live model call has been made yet** (see below) |
| Web app: home, `/jobs` search, `/sign-in`, `/trust` (what this server sends data to), `/control/costs` (model cost), light and dark themes | **Working**, with automated accessibility checks on each screen in both themes |
| Checks that no resume, chat, contact or query text reaches a log, a metric or a trace | **Working** (a test that fails when one does) |
| Evaluation harness with a regression gate | **Working.** Two gold sets exist (400 location inputs and 50 job postings with their requirements), both with labels drafted by an agent, review pending. The gate in CI replays recorded answers: a replay shows that the code path works and is **not** a quality result, and no live model has been measured |
| Continuous integration (`.github/workflows/`) | **Written and run on a developer machine** (`scripts/ci-local.ps1`); it has not yet run on GitHub |
| Job index from public Greenhouse, Lever and Ashby boards, closed-job detection, fetch history, duplicate folding | **Working** (the index is empty until you fill it, see below). An index made before phase 2 is converted in place by one migration: back it up and rehearse it on a copy first (`db/rehearse_upgrade.py`) |
| Search by meaning and by keywords, with country and remote filters; the board each job came from and when it was last seen open on every result and job page | **Working**, with no model key. `/jobs` and `GET /v1/jobs` take a plain-English query and fuse a keyword ranking with a vector ranking from a local embedding model. A test with invented postings and the real model shows a query finding a posting that shares no word with it. While no job has a vector yet, or the model cannot load, search falls back to keywords and the page says so. There is **no search-quality gold set**, so no search quality is claimed |
| Scheduled refresh and apply-link re-check | **Working.** The worker refreshes every active board every 6 hours with no command, and a job that leaves its board is closed. `python -m app.cli jobs verify JOB_ID` asks a job's own apply link through a guarded fetch (it refuses private addresses) and closes the job on a 404 or 410. It is run by hand: nothing calls it before a brief is built (phase 4) |
| Bulk board import | **Working.** `python -m app.cli sources import FILE` (one board name per line, `--dry-run` writes nothing) registers only boards that answer with live postings |
| Location parser: country and city read from the posting's text, and left unknown when it does not say | **Working.** On 260 test rows of a 400-row labelled sample, 258 were read correctly (0.9923, 95% interval 0.9724 to 0.9979), and the parser gave a place for no row whose label says there is none; labels drafted by an agent, review pending. Only 4 rows have no country label and 46 no city label, so that second result is a weak check, not a guess rate. 40 more test rows are left out because their text is also in the parser's tests or the labelling guideline ([`evals/location/README.md`](evals/location/README.md)). A place that cannot be read shows "Location: unknown" |
| Requirement extraction: each open job's requirements as cited items, required or preferred, with the exact words marked on the job page | **Built, and off until a model is proven.** It runs only with a model key **and** a passed live gold-set run for that model and prompt. No live run exists, so no job is extracted and job pages say the requirements are not extracted yet. The 50-posting gold set has labels drafted by an agent, review pending, and no extraction quality is claimed ([`evals/requirement_extraction/README.md`](evals/requirement_extraction/README.md)) |
| System design: overview, requirements, roadmap, architecture, data model, API, AI system, front end | **Published** in [`docs/`](docs/OVERVIEW.md); the data model's schema and integrity rules were executed and tested on PostgreSQL |
| Career record, matching with cited evidence, morning brief | Planned (phases 3 and 4) |
| Chat agent, resume tailoring that cannot invent, application tracking | Planned (phase 5) |
| Hiring desk: roles, consent-first introductions, answer clock | Planned (phase 6) |
| Done-for-you campaigns, Guardian, autonomy dial, flight recorder | Planned (phase 7) |
| Simulated-market evaluation ("wind tunnel") and scoreboard | Planned (phase 8) |
| Twin and MCP agent interface | Planned (phase 9) |

### First run, 22 November 2026

Measured on a laptop, from 185 candidate board names:

| Measure | Value |
|---|---|
| Live boards found | 71 |
| Open postings ingested | 11,357 |
| Unique jobs after folding same-company duplicates | 9,996 |
| Unique open jobs in India | 1,019 across 43 employers |
| Jobs whose country could not be recognised (stored as unknown, never guessed) | 3,552 |
| One keyword search with a country filter | about 0.1 s (single measurement) |

## Run it locally

Requirements: Docker with Compose. Running the tests also needs [uv](https://docs.astral.sh/uv/) (Python 3.11) and Node 22 with pnpm.

```bash
cp .env.example .env
docker compose up
```

The first start builds two images, so it takes a few minutes. When it settles:

| What | Where |
|---|---|
| The app | http://localhost:8080 |
| Mail catcher (sign-in codes arrive here) | http://localhost:8025 |
| PostgreSQL 17 with pgvector | 127.0.0.1:5433 |

Every port is published on `127.0.0.1` only, so nothing is reachable from another machine. `.env.example` holds development-only keys and passwords that are public in this repository; the server refuses them as soon as `APP_BASE_URL` is not a localhost address. Change a port in `.env` (`APP_HTTP_PORT`, `APP_MAIL_PORT`, `APP_DB_PORT`) if one is taken.

### Sign in

Open http://localhost:8080/sign-in, enter any email address and choose Send code. Open the mail catcher at http://localhost:8025, read the six-digit code and type it in. The session lasts until you sign out. The mail catcher keeps the email in memory; nothing is sent to the outside.

### Fill the job index

The index starts empty. Ask the worker to find the live boards, wait until the discovery job has finished, then fetch them:

```bash
docker compose exec worker python -m app.cli sources discover
docker compose exec worker python -m app.cli queue list        # wait until ingest.discover shows succeeded
docker compose exec worker python -m app.cli ingest run
```

Jobs appear at http://localhost:8080/jobs as the fetches finish. A worker runs one fetch at a time; a pass over the 71 boards took under two minutes when this was checked on 22 November 2026. From then on the worker fetches every board again every 6 hours, and gives new and changed jobs their vectors in batches of 500 every five minutes, so a large index takes some time to become searchable by meaning (keyword search works from the start).

### With no model key

Sign-in, the job index and search need no key. Anything that needs a language model answers `503 ai_disabled`. `.env.example` sets `LLM_PROVIDER=fake`: a built-in provider that replays recorded answers, makes no network call and costs nothing, so the screens that show model use (`/trust`, `/control/costs`) can be seen without a key. Its cost rows read zero because nothing was paid. It has an answer only for a task that has a recording, and none of the resume tasks has one here, so a resume you upload is refused as `ai_disabled` within a second (the three resume tasks read a real model only after a passed run on the resume gold set: see `evals/resume_extraction/facts/README.md`).

### Use a real model

Set these in `.env`, then run `docker compose up -d`:

| Provider | Settings |
|---|---|
| Anthropic | `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` |
| OpenAI | `LLM_PROVIDER=openai` and `OPENAI_API_KEY` |
| A local model through Ollama | `COMPOSE_PROFILES=dev,local-llm`, `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL=http://llm:11434/v1`; put the model's real name in `backend/config/models.toml` (it holds a placeholder) and pull it with `docker compose exec llm ollama pull <name>` |

**Not checked against a live provider yet.** No provider key was available while this was built: the three adapters have only run against stub clients, and the local-model route has not run at all. The first check, once you have a key:

```bash
docker compose exec worker python -m app.platform.llm ping --email you@example.com
```

Then sign in as that address, open http://localhost:8080/control/costs and confirm a row for the task `ping` with a cost above zero.

### Tests and checks

```bash
cd backend && uv run pytest                  # needs the database: docker compose up -d db
cd web && pnpm install && pnpm test --run    # also pnpm typecheck and pnpm lint
powershell -File scripts/ci-local.ps1        # every gate of the CI workflow, one PASS or FAIL line each
powershell -File scripts/fresh-clone-check.ps1 -WithBrowserTests
```

The backend tests use a separate database named `app_test` on the same server and refuse any database whose name does not end in `_test`, so the data in `app` is never touched. `fresh-clone-check.ps1` clones the committed branch into a temporary folder, starts it as its own Compose project on other ports, checks it, runs the browser tests (Playwright: sign in with a code read from the mail catcher, reload, theme switch, search, a model cost row, sign out, and an accessibility scan of every screen in both themes) and deletes the copy; your own stack and database are not touched. The two scripts are PowerShell and were written on Windows. To point the browser tests at a stack you started yourself, set `BASE_URL` and `MAILPIT_URL` (see `web/playwright.config.ts`); they create accounts and write cost rows, so use a throwaway stack. The job specs also add invented jobs to it, and refuse to unless `COMPOSE_PROJECT_NAME` names that stack's Compose project and is not `openelip`, the name your own stack has by default (`E2E_COMPOSE_DIR` is the folder `docker compose` runs in).

### Stop

`docker compose down` stops everything and keeps your data in the `pgdata` volume. `docker compose down -v` also deletes that volume, and the database with it.

### Layout

```
backend/    Python package `app`: platform (config, database, queue, sign-in, model gateway, telemetry), ingest (job index), trust, evals; Dockerfile
web/        Next.js app: screens, design tokens, typed API client; Dockerfile; browser tests in tests/e2e
db/         Alembic migrations, the only source of the schema
evals/      gold sets, recorded answers and floors for the evaluation harness
data/       candidate board names that discovery starts from
docs/       the design set
scripts/    ci-local.ps1 and fresh-clone-check.ps1
.github/    CI workflows: ci.yml on pull requests, e2e.yml nightly
docker-compose.yml, Caddyfile, .env.example
```

## Rules the project holds itself to

1. A job that violates a stated hard constraint is never shown as a match.
2. Every shown claim resolves to real text in the job and a confirmed fact in the record.
3. A resume edit cannot add experience the record does not contain.
4. Nothing about a candidate reaches an employer or an outside agent without that candidate's action for that employer and role.
5. No outside action runs unless the policy engine and the Guardian both pass it, and every such action is logged.
6. No message, brief or introduction is sent twice.
7. Deletion removes a person from every store the system controls.
8. No quality claim is published unless a recorded evaluation run produced it.

Unknown values stay unknown. Nothing is guessed to fill a gap.

## Roadmap

| Phase | Outcome |
|---|---|
| 1 | Platform and web shell: one-command stack, sign-in, model gateway with cost tracking, queue, CI |
| 2 | Searchable job index: keyword plus semantic search, scheduled refresh, requirement extraction |
| 3 | Career record: resume to confirmed facts with source lines, constraints, change log, export and delete |
| 4 | Matches and morning brief: at most five safe matches a day with cited evidence |
| 5 | Chat agent, verified resume tailoring, application tracking |
| 6 | Hiring desk: roles, consent-first introductions, said-yes shortlist, answer clock |
| 7 | Done-for-you campaigns under the Guardian |
| 8 | Mission control and the simulated-market evaluation |
| 9 | Twin, MCP agent interface, release |

## Stack

Python, FastAPI, PostgreSQL 17 (full-text search and pgvector), Next.js 16 with React 19 for the web app, a provider-agnostic model gateway, local embeddings (fastembed with `BAAI/bge-small-en-v1.5` on the CPU, no key), Caddy as the one entry point, Docker Compose. Planned: LangGraph for the campaign graph, approval pauses and agent loops, with its state checkpointed in PostgreSQL. No vector database product, no knowledge graph, no hosted agent platform: one database, and plain code wherever plain code is enough.

Start with [`docs/OVERVIEW.md`](docs/OVERVIEW.md) for the design set.

## How this is built

Specifications are written first, then AI coding agents implement them phase by phase, and each phase ships with tests. The maintainer directs the design and reviews the output.

## Data sources and conduct

Only public, documented job-board endpoints are read, at a low request rate. The project does not scrape sites behind a sign-in and does not collect people's profiles.

## Licence

MIT. See [LICENSE](LICENSE).
