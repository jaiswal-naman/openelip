# OpenElip

An open-source, self-hosted agent hiring marketplace that runs on your own machine.

> **Independent project.** OpenElip is inspired by the agent-marketplace idea that Elip describes publicly. It is not affiliated with, endorsed by, or built from any code or assets of Elip or Freshview Innovations, Inc.

## What it is

Most job tools hand you a list. OpenElip is being built to do the work behind the list and to show its evidence:

- **Real jobs only.** Postings come from employers' own public job boards, are checked on every refresh, and are closed when they leave the board.
- **Dealbreakers enforced in code.** A job that breaks a stated hard constraint is filtered by the database before any model sees it.
- **Reasons you can check.** Each match is a requirement-by-requirement table that points at the exact sentence in the job post and the exact line in your resume.
- **Done for you, decided by you.** A crew of agents researches the company, prepares the application and drafts follow-ups. Nothing leaves the system without your yes.
- **Local first.** One `docker compose up`. Search runs with no API key. Language-model features use a key you supply, or a local model through an OpenAI-compatible endpoint.

## Status

This project is early. The table says exactly what runs today.

| Area | State |
|---|---|
| Job index from public Greenhouse, Lever and Ashby boards | **Working** |
| Closed-job detection, fetch history, duplicate folding | **Working** |
| Keyword search API with country and remote filters | **Working** |
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

Requirements: Docker, and [uv](https://docs.astral.sh/uv/) for Python.

```bash
docker compose up -d            # PostgreSQL 17 with pgvector on 127.0.0.1:5433
uv sync                         # install dependencies
uv run python -m app.discover   # test candidate board names, register the live ones
uv run python -m app.ingest     # fetch jobs, close missing ones, fold duplicates
uv run uvicorn app.api:app --port 8000
```

Then open `http://127.0.0.1:8000/v1/jobs?q=backend%20engineer&country=IN` or `http://127.0.0.1:8000/docs`.

Run the tests with `uv run pytest -q`.

The database password in `docker-compose.yml` is a local development default and the port is bound to `127.0.0.1` only. Change it before exposing the database to anything else.

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

Python, FastAPI, PostgreSQL 17 (full-text search and pgvector), Docker Compose today. Planned: Next.js for the web app, local embeddings, a provider-agnostic model gateway, and LangGraph for the campaign graph, approval pauses and agent loops, with its state checkpointed in PostgreSQL. No vector database product, no knowledge graph, no hosted agent platform: one database, and plain code wherever plain code is enough.

Start with [`docs/OVERVIEW.md`](docs/OVERVIEW.md) for the design set.

## How this is built

Specifications are written first, then AI coding agents implement them phase by phase, and each phase ships with tests. The maintainer directs the design and reviews the output.

## Data sources and conduct

Only public, documented job-board endpoints are read, at a low request rate. The project does not scrape sites behind a sign-in and does not collect people's profiles.

## Licence

MIT. See [LICENSE](LICENSE).
