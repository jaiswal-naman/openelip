# OpenElip

Working name. The public name is still an open decision (see Key decisions). This is an independent project. It is not affiliated with, endorsed by, or built from any code or assets of Elip or Freshview Innovations, Inc.

## How to read the design set

- [OVERVIEW.md](OVERVIEW.md) (this file): what the project is, the canonical names, the invariants, the constraints and the key decisions.
- [REQUIREMENTS.md](REQUIREMENTS.md): every v1 requirement with its id, the v2 list, what is out of scope, and which phase covers each requirement.
- [ROADMAP.md](ROADMAP.md): the nine build phases, with goals, success criteria and a demo for each.
- [ARCHITECTURE.md](ARCHITECTURE.md): the runtime shape: processes, modules, flows, background work, security, privacy, operations and testing.
- [DATA_MODEL.md](DATA_MODEL.md): tables, constraints, triggers and row-level security.
- [API.md](API.md): REST endpoints, server-sent event streams, the MCP server and error codes.
- [AI_SYSTEM.md](AI_SYSTEM.md): the model gateway, retrieval, the agent crew, the Guardian, the Twin and evaluation.
- [FRONTEND_UI.md](FRONTEND_UI.md): every screen, the design system, accessibility and the front-end architecture.

Where the documents refer to "the design review", they mean a cross-document review held on 2026-11-22 that settled conflicts between these documents; its outcomes are already applied in each of them.

## What this is

OpenElip is an open-source, self-hostable agent hiring marketplace: a career agent for the candidate, a hiring agent for the company, and introductions both sides agree to. It is inspired by the agent-marketplace idea Elip describes publicly. It extends that loop with five layers: a trust core, done-for-you campaigns run by a crew of agents, a Twin that answers screening questions from the candidate's record, a market with scarce signals and a guaranteed answer, and an evaluation system that tests the agents in a simulated job market.

It is for two kinds of user: a candidate who wants the job search run for them with their approval at each decision, and an employer who wants interviews with people who have already said yes, each with evidence.

## Core value

Every match and every agent action is backed by evidence the user can check, and nothing leaves the system without the user's yes.

## Current state

A foundation slice works today: it ingests postings from public Greenhouse, Lever and Ashby job boards and records every fetch (IDX-01), closes a job when it leaves its board but never on a failed or suspiciously empty fetch (IDX-02), folds same-company duplicate postings onto one canonical job (IDX-03), and offers keyword search over open, de-duplicated jobs with country and remote filters. First run: 71 live boards, 11,357 postings, 9,996 unique jobs, 1,019 in India, search about 0.1 s. Everything else in this design set is a proposal. What is out of scope, and why, is listed in [REQUIREMENTS.md](REQUIREMENTS.md).

## System shape (canonical names)

These names are binding for every design document, plan and line of code.

**Stack.** Backend: Python 3.11+, FastAPI, Pydantic, SQLAlchemy 2, Alembic, psycopg 3. Database: PostgreSQL 17 with pgvector and built-in full-text search; it is also the queue and the outbox. Web: Next.js (App Router, TypeScript). Agents: LangGraph (campaign graph, approval interrupts, chat and Researcher loops) with a PostgreSQL checkpointer, used only as an orchestration library: no hosted platform, tracing off. Embeddings: local by default (384 dimensions) so search runs with no API key. One embedding model serves an instance; changing it is a migration that re-embeds every row. Models: reached only through our own gateway; Anthropic is the default provider, with at least one other provider and an OpenAI-compatible local endpoint supported. Packaging: Docker Compose for the whole stack. No vector database product, no knowledge graph, no multi-agent chat framework (LangGraph runs our graphs; agents do not converse with each other).

**Repository layout.** `backend/` (Python package `app`), `web/` (Next.js), `evals/` (gold sets, scenarios, reports), `docs/` (public design documents), `db/` (migrations), `docker-compose.yml`.

**Backend modules** (`backend/app/<module>/`):

| Module | Responsibility |
|---|---|
| `platform` | Auth, API shell, model gateway, queue, outbox, scheduler, storage, observability |
| `trust` | Consent, audit log, export, deletion |
| `ingest` | Job index: sources, fetchers, normalizer, deduplicator, liveness, requirement extraction |
| `profile` | Career Record: documents, facts, constraints, preferences, change log |
| `match` | Constraint filter, retrieval, evidence scoring, ranking, decision ledger |
| `brief` | Morning brief: composition, email and in-app delivery |
| `chat` | Conversation agent; reads freely, writes only as proposals |
| `tailor` | Resume edit proposals, verifier, export |
| `apply` | Application tracker and application packets |
| `hiring` | Organizations, roles, shortlist, introductions, answer clock, interest signals |
| `campaigns` | Durable campaign state machine and the agent crew |
| `guardian` | Policy engine, autonomy settings, verifier checks, flight recorder, undo window |
| `twin` | Grounded answers to employer questions inside consent scopes |
| `agentnet` | MCP server and consent-scoped agent tokens |
| `evals` | Gold sets, judges, benchmark, wind tunnel, scoreboard |

**Agent crew** (roles of one runtime, not separate frameworks): Scout, Researcher, Analyst, Writer, Operator, Chaser, Coach, Twin, Guardian. In v1 the Scout is the scheduled match run; only the Researcher and the chat agent run a free tool loop; the other roles are fixed workflows; the Guardian is code (a policy engine and checks), and a model-based check can only veto. The crew member is "the Operator role"; the person who runs an installation is "the instance operator". Campaigns are a LangGraph graph, one thread per campaign; the roles still share the campaign board in our tables (state in Postgres) and do not message each other.

**Core entities.** Identity and trust: `users`, `sessions`, `login_codes`, `consents`, `audit_log`, `data_requests`. Career Record: `documents`, `profile_facts`, `constraints`, `preferences`, `profile_events`. Job index: `job_sources`, `jobs`, `job_requirements`, `fetch_runs`, `skills`. Matching: `match_runs`, `match_decisions`, `match_evidence`, `feedback_events`. Delivery: `briefs`, `brief_items`, `outbox`. Tailoring and applying: `tailored_resumes`, `resume_edits`, `applications`, `application_events`, `application_packets`. Hiring: `organizations`, `org_members`, `roles`, `role_requirements`, `introductions`, `shortlist_entries`, `interest_signals`. Campaigns and control: `campaigns`, `campaign_steps`, `company_dossiers`, `agent_runs`, `agent_actions`, `approvals`, `autonomy_settings`, `guardian_checks`. Twin and network: `twin_scopes`, `twin_sessions`, `twin_messages`, `agent_tokens`. Conversation: `chat_sessions`, `chat_messages`. Platform and evals: `llm_calls`, `model_registry`, `queue_jobs`, `idempotency_keys`, `eval_runs`, `eval_items`, `sim_scenarios`, `sim_runs`, `sim_results`.

**API.** REST under `/v1`, JSON, one envelope (`success`, `data`, `error`, `meta`), cursor pagination, idempotency keys on writes, server-sent events for streams. People authenticate with a session cookie; agent tokens are accepted only at `/mcp`. Groups: `/v1/auth`, `/v1/me` (account, Career Record, the candidate's agent tokens), `/v1/jobs`, `/v1/stats`, `/v1/matches`, `/v1/decisions`, `/v1/briefs`, `/v1/chat`, `/v1/tailored-resumes`, `/v1/applications`, `/v1/campaigns`, `/v1/approvals`, `/v1/autonomy`, `/v1/twin`, `/v1/orgs` (members, answer clock, the organization's agent tokens), `/v1/roles`, `/v1/introductions`, `/v1/signals`, `/v1/control` (runs, flight recorder, costs, evals, the live event stream), `/v1/privacy`, `/v1/webhooks` (email provider events). Outside `/v1`: `GET /healthz` and the MCP server at `/mcp`.

**Web routes.** Candidate: `/onboarding`, `/app/today`, `/app/campaigns`, `/app/campaigns/[id]`, `/app/matches/[id]`, `/app/matches/[id]/tailor`, `/app/record`, `/app/twin`, `/app/settings`. Hiring desk: `/hire/roles`, `/hire/roles/[id]`, `/hire/shortlist/[roleId]`, `/hire/conversations`, `/hire/answers`, `/hire/team`. Mission control (the signed-in person's own data; the eval scoreboard is readable by any signed-in user): `/control/runs`, `/control/recorder`, `/control/evals`, `/control/costs`. Public: `/`, `/jobs`, `/trust`, `/sign-in`, `/unsubscribe`.

## Invariants that are never traded away

1. A job violating a stated hard constraint is never shown as a match.
2. Every shown claim resolves to its source: real text in the job, a confirmed fact in the record, or a quote that was present on a fetched public page when the claim was made.
3. A resume edit cannot add experience the record does not contain.
4. Nothing about a candidate reaches an employer or outside agent without that candidate's action: accepting an introduction for that employer and role (which also covers Twin answers within the candidate's autonomy and topic settings), or issuing a token to their own agent.
5. No outside action runs unless the policy engine and the Guardian both pass it, when it is cleared and again when it runs; every attempt, including a blocked one, is logged.
6. No message, brief or introduction is sent twice.
7. Deletion removes a person from every store we control.
8. No quality or performance number is shown or published unless a recorded eval run produced it; figures read from the ledgers (costs, counts, timings) are labelled as measured operational data.

An outside action is something an agent does that reaches anyone other than the candidate: in v1, preparing an application for submission, sending a follow-up email, and a Twin answer. The candidate's own clicks (accepting an introduction, sending a signal, replying to a Twin question) are recorded in the audit log and are never proposed by an agent.

## Constraints

- **Legal and ethical**: public, documented job-board endpoints only; no scraping behind sign-ins; no other company's code, assets or branding.
- **Explainability**: every part should be explainable by reading it; prefer plain, well-named code and small modules over clever abstractions.
- **Self-hostable**: `docker compose up` must give a working product; search must work with no model API key.
- **Cost**: bring-your-own model key; every model call costed and attributed; a per-campaign budget the Guardian enforces.
- **Honesty**: unknown values stay unknown (never guessed); mock or illustrative data is labelled; measured numbers only.

## Key decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Build an independent open-source implementation of the agent hiring loop, then extend it | A running, measured system is stronger proof than documents or mock-ups | Pending |
| Public project name | The working name borrows another company's brand. Recommended: a distinct name, with a note that the project is independent and not affiliated | Accepted as recommended (owner, 2026-11-22) |
| Modular monolith on one PostgreSQL | Correctness lives in the database (unique keys, triggers, row-level security); one deploy; easy to explain | Pending |
| Local embeddings by default | Anyone can run search with no key; matches the existing `vector(384)` column | Pending |
| Agents run on LangGraph, used in a bounded way: campaigns are a LangGraph graph with a PostgreSQL checkpointer, one thread per campaign, resumed and timed by queue jobs; the policy engine and the Guardian stay our own code | Autonomy without losing control; our tables stay the source of truth, so every step is auditable and replayable; no hosted platform, tracing off | Adopted (owner's decision, 2026-11-22) |
| Agents share a board instead of chatting | Lower cost, no hidden state, simpler to test | Pending |
| MIT licence | Signals goodwill; simplest for others to adopt | Accepted as recommended (owner, 2026-11-22) |
| Only three outside action types in v1: `submit_application` (prepared only; the candidate submits), `send_follow_up`, `twin_answer` | Everything else is the candidate's own click (accepting, signalling, replying to the Twin), internal work (research, drafting) or a recorded state change; fewer types means fewer paths through the two-key rule | Adopted (design review, 2026-11-22) |
| People sign in with a session cookie behind one reverse proxy; bearer tokens only for agent tokens at `/mcp` | The proxy routes `/v1/*` and `/mcp` to the API and everything else to the web app, so `EventSource` works with the cookie and no cross-origin calls are needed; CSRF is covered by `SameSite=Lax`, JSON-only writes and the required `Idempotency-Key` | Adopted (design review, 2026-11-22) |
| No admin flag and no operator web view in v1 | The instance operator acts only through `python -m app.cli` and environment configuration, sees no candidate content, and CLI actions are audited with `actor_type` `operator`; mission control shows each person's own data | Adopted (design review, 2026-11-22) |
| Accepting an introduction is one transaction with no undo | Consent, status, answer clock, shortlist entry equal to the frozen `share_preview`, a campaign at `applied` and the organization's email happen together; the safeguard is the preview and its hash, shown before accepting | Adopted (design review, 2026-11-22) |
| Agent tokens have two kinds of holder | Candidate tokens (`matches:read`, `explanations:read`) and organization tokens issued by an owner or admin (`twin:ask`, `introductions:request`); accepted only at `/mcp`; pre-issued, OAuth is v2 | Adopted (design review, 2026-11-22) |
| G1: Should the platform email outside recruiters when the candidate approves a follow-up, or only hand over a draft? | Recommended default: send, only after each approval, only to an address the candidate typed, replies going to the candidate, at most two per campaign, with the ten-minute undo | Accepted as recommended (owner, 2026-11-22) |
| G2: Should onboarding offer one switch that lets the Twin answer from the confirmed record without asking? | Recommended default: keep the safe defaults and offer the switch (Twin answers "tell me after"; experience, skills, education and projects "answer"); pay and contact stay closed | Accepted as recommended (owner, 2026-11-22) |
| G3: Should deleting an account also delete the employer's view of that person's introductions and answers? | Recommended default: delete everything on the instance in v1, say on `/trust` that copies made outside the system cannot be reached, and get legal advice before running a hosted service | Accepted as recommended (owner, 2026-11-22); needs legal advice |
| G4: Should REC-07 also cover raw chat and resume text? | Recommended default: limit REC-07 to the Career Record (facts, constraints, preferences, proposals); raw text stays as written, visible only to the person, exported and deleted with the account | Accepted as recommended (owner, 2026-11-22) |
| G5: Should a small wording edit to a posting count as a change that may show the job again? | Recommended default: yes in v1, always show what changed, and measure it; add a separate "material change" version only if re-shows exceed 2% of shown matches (assumption) | Accepted as recommended (owner, 2026-11-22) |
| G6: Below what count does the anonymous preview hide numbers? | Recommended default: 5 | Accepted as recommended (owner, 2026-11-22) |
| G7: Can an employer send a serious-interest signal before the candidate accepts? | Recommended default: not in v1; employers signal only on accepted introductions; revisit with MKT-01 | Accepted as recommended (owner, 2026-11-22) |
| G8: Should the web app show all users' runs and costs to the instance operator? | Recommended default: defer to v2; the instance operator uses the command line | Accepted as recommended (owner, 2026-11-22) |
| G9: Should nightly scoring use a provider's half-price batch service (the default provider keeps batch requests for 29 days)? | Recommended default: off by default; an instance setting the operator can turn on, disclosed on `/trust`, for de-identified text only | Accepted as recommended (owner, 2026-11-22) |

Owner decisions G1 to G9 came out of the design review. Until the owner says otherwise, every design document, plan and requirement assumes the recommended default.
