# Requirements: OpenElip

**Defined:** 2026-11-22
**Core Value:** Every match and every agent action is backed by evidence the user can check, and nothing leaves the system without the user's yes.

## v1 Requirements

Requirements for the first release. Each maps to exactly one roadmap phase. Items marked (validated) already work in the foundation slice and are listed for completeness; they are not re-planned.

### Platform

- [x] **PLT-01**: A developer can start the whole stack locally with one command and reach the web app and the API
- [x] **PLT-02**: A user can sign in with an emailed one-time code, stays signed in across a refresh, and can sign out
- [x] **PLT-03**: Every write endpoint that uses the session cookie accepts an idempotency key and replays the first response for a repeated key; the four public writes that carry their own proof instead (sign-in code request, sign-in, unsubscribe link, webhook receiver) are exempt (API.md 1.6)
- [x] **PLT-04**: Every model call goes through one gateway that validates structured output and records task, model, tokens, cost and latency
- [x] **PLT-05**: The instance operator can switch the model provider per task by configuration (Anthropic by default, one other provider, and an OpenAI-compatible local endpoint)
- [x] **PLT-06**: Background jobs run from a Postgres-backed queue with retries, a dead-letter state, per-job unique keys and scheduled jobs
- [x] **PLT-07**: Logs and traces carry ids and timings but never resume, chat or contact text, enforced by a test
- [x] **PLT-08**: CI runs lint, type checks, tests, a migration check and the eval regression gate on every pull request

### Job Index

- [x] **IDX-01**: The system ingests postings from public Greenhouse, Lever and Ashby boards and records every fetch (validated)
- [x] **IDX-02**: A posting that leaves its board is closed and is never shown as open (validated)
- [x] **IDX-03**: Same-company duplicate postings fold onto one canonical job (validated)
- [x] **IDX-04**: A user can search open jobs by meaning as well as keywords, with the two rankings fused, and filter by country and remote
- [x] **IDX-05**: Every job shows its source board and when it was last verified open
- [x] **IDX-06**: Country and city are recognised for at least 95% of postings on a labelled sample, and unknown values stay unknown
- [x] **IDX-07**: The index refreshes on a schedule and re-checks a job's apply link before the job appears in a brief
- [x] **IDX-08**: Each job's requirements are extracted once per content version as discrete items with character spans, marked required or preferred
- [x] **IDX-09**: The instance operator can bulk-import candidate board names from the command line, and only boards that answer with live postings are registered

### Career Record

- [x] **REC-01**: A candidate can upload a resume (PDF or DOCX) and see extracted facts, each showing the resume line it came from
- [x] **REC-02**: A candidate can confirm, correct or reject each extracted fact, and only confirmed facts are used anywhere
- [x] **REC-03**: A candidate can state hard constraints (location, remote policy, work authorization, salary floor, employment type, seniority, excluded companies) through a short guided intake and confirm each
- [x] **REC-04**: A candidate can state soft preferences, and a dealbreaker that cannot be typed is stored as a soft preference with a notice that it is not guaranteed
- [x] **REC-05**: A candidate can view the whole record on one page, edit any field, and see every change in a log with before, after, source and time
- [x] **REC-06**: Hidden or off-page text in an uploaded resume is stripped and flagged to the candidate
- [x] **REC-07**: Protected characteristics are never stored in the Career Record (facts, constraints, preferences and proposals), even when volunteered in conversation or found in a resume (wording follows the recommended answer to owner decision G4, still pending)

### Matching

- [x] **MAT-01**: No job that violates a stated hard constraint is ever shown as a match, proven by a property test over at least 10,000 generated cases and an independent checker on every run
- [x] **MAT-02**: A candidate sees at most five ranked matches a day, and zero is allowed
- [x] **MAT-03**: Each match shows a requirement-by-requirement table with a verdict (met, partly met, not met, no evidence), the quoted job text and the cited record fact
- [x] **MAT-04**: A candidate can select a table row and see the exact job sentence and resume line highlighted
- [x] **MAT-05**: A verdict whose citation does not resolve is rejected by the database and its job is withheld
- [x] **MAT-06**: A candidate can open "why not" and see withheld and filtered jobs with reason codes
- [x] **MAT-07**: A candidate never sees the same job twice unless it materially changed, and is told what changed
- [x] **MAT-08**: A candidate can save or dismiss a match with a reason, and a dismissal reason can propose a record change that the candidate confirms

### Brief

- [x] **BRF-01**: A candidate receives one morning brief per day by email and in the app by 07:00 local time, never twice
- [x] **BRF-02**: The brief lists the decisions that need the candidate (approve, edit, skip) and what was done since the last brief
- [x] **BRF-03**: A candidate can set the brief to daily, weekly or paused, and unsubscribe in one click

### Chat

- [x] **CHT-01**: A candidate can ask the agent about their record, matches and any decision, and gets answers that cite the ledger
- [x] **CHT-02**: The agent can search the index on request with the candidate's hard constraints applied
- [x] **CHT-03**: The agent can propose a record change, and nothing changes until the candidate confirms
- [x] **CHT-04**: Instructions embedded in job text or resumes are never followed, proven by an injection test suite

### Tailoring

- [x] **TLR-01**: A candidate can request a tailored resume for a job and sees proposed edits as a per-edit diff
- [x] **TLR-02**: Edits that add unsupported skills, numbers, dates, titles or employers are discarded before the candidate sees them, and the number discarded is shown
- [x] **TLR-03**: A candidate can approve or reject each edit and export the result as PDF and DOCX

### Applications

- [x] **APP-01**: A candidate can track an application's status (applied, interview, offer, rejected, no response) with timestamps
- [x] **APP-02**: A candidate gets a prepared application packet per job (tailored resume, answers to common questions, a short note) to submit themselves

### Hiring Desk

- [x] **HIR-01**: An employer member can create an organization and invite members, and one organization cannot read another's data, proven by a row-level security test
- [x] **HIR-02**: An employer can describe a role in text and confirm the extracted requirements, and a role cannot go live without a pay range
- [x] **HIR-03**: An employer sees an anonymous preview (counts only) of matching candidates before anyone has accepted
- [x] **HIR-04**: A matching candidate is asked privately, sees exactly what the employer would receive before accepting, and no reply within seven days counts as no
- [x] **HIR-05**: An employer's shortlist contains only candidates who accepted that role, each with the requirement evidence table
- [x] **HIR-06**: An employer must answer each introduction with yes or no and a reason within five business days, an answer clock shows what is owed, and overdue answers pause new introductions
- [x] **HIR-07**: A candidate can block named employers in advance
- [x] **HIR-08**: Each side can send a limited number of "serious interest" signals per week, and the other side sees them

### Campaigns

- [ ] **CMP-01**: When a candidate approves a match or accepts an introduction, a campaign starts and moves through visible stages (researching, needs you, applied, talking, interview, offer, closed) on a board
- [ ] **CMP-02**: The Researcher agent produces a cited one-page company dossier for each campaign
- [ ] **CMP-03**: The Writer and Operator agents prepare the application packet and the candidate approves it with one action
- [ ] **CMP-04**: The Chaser agent drafts a follow-up when a campaign has had no update for a configured number of days, nothing is sent before the candidate approves, and it goes only to an address the candidate entered
- [ ] **CMP-05**: The Coach agent produces interview preparation built from the candidate's evidence gaps and the dossier
- [ ] **CMP-06**: A campaign survives restarts and waits for days without losing state, proven by a kill-and-resume test
- [ ] **CMP-07**: A candidate can pause or cancel any campaign at any time

### Guardian

- [ ] **GRD-01**: A candidate can set, per outside action type, ask-first, tell-me-after or never where that type allows it; follow-ups and applications never proceed without approval, and no agent can share a profile
- [ ] **GRD-02**: No outside action executes unless the policy engine and the Guardian checks both pass, and a blocked action is recorded with its reason
- [ ] **GRD-03**: Every agent step is recorded in a flight recorder with its inputs, cited evidence, cost and outcome, and can be replayed in the app
- [ ] **GRD-04**: An approved follow-up email waits in an undo window (ten minutes by default) before sending
- [ ] **GRD-05**: A candidate can see model cost per campaign and per month
- [ ] **GRD-06**: Mission control shows each agent's live status for a campaign
- [ ] **GRD-07**: The Guardian pauses a campaign that reaches its model budget or shows signs of a scam posting, records the reason, and the candidate can raise the budget and resume, or cancel

### Twin

- [ ] **TWN-01**: After a candidate accepts an introduction, the employer can ask the candidate's Twin questions and gets answers that cite record facts
- [ ] **TWN-02**: When the record lacks the answer, the Twin says so, asks the candidate, and relays the answer only after the candidate replies
- [ ] **TWN-03**: A candidate sets which topics the Twin may answer and which never, and current pay and contact details are never answered
- [ ] **TWN-04**: A candidate can read every Twin question and answer

### Agent Network

- [ ] **NET-01**: An external AI agent can connect over MCP with a consent-scoped token: a candidate's own agent can search that candidate's matches and read explanations, and an employer's agent can ask the Twin inside an accepted introduction
- [ ] **NET-02**: An employer's agent can request introductions for one of its open roles, and each takes effect only after the candidate accepts
- [ ] **NET-03**: Out-of-scope calls are refused and logged

### Evals

- [x] **EVL-01**: Gold sets for resume extraction, requirement extraction, evidence faithfulness and tailoring integrity run in CI on every prompt or model change
- [x] **EVL-02**: A labelled benchmark of resume and job pairs compares at least four matching methods on quality and cost
- [ ] **EVL-03**: A simulated market (the wind tunnel) runs whole campaigns against scripted employer personas (silent, lowball, scam, hidden instruction, and a cooperative control) and reports outcomes and violations
- [ ] **EVL-04**: Each scenario runs five times and passes only if all five pass, and results appear on a scoreboard in mission control
- [ ] **EVL-05**: No quality or performance number appears in the app or the README unless a recorded eval run produced it; operational figures from the ledgers are labelled as such

### Trust

- [x] **TRU-01**: A candidate can export all their data as a machine-readable archive
- [x] **TRU-02**: A candidate can delete their account, all rows, files and embeddings are removed, and a registry test fails if a table with user data has no deleter
- [ ] **TRU-03**: A candidate can read an audit log of every automated decision about them
- [x] **TRU-04**: A public page lists every model provider and sub-processor in use
- [x] **TRU-05**: Before any model processes a candidate's data, the candidate agrees to AI processing with the providers named; without that consent search, the record and the ledger still work and AI features say why they are off

### Web App

- [ ] **UI-01**: The candidate app has Today, Campaigns, Record, Twin and Settings; the hiring desk has Roles, Shortlist, Conversations and Answers; mission control has Runs, Recorder, Evals and Costs
- [ ] **UI-02**: Every screen works at phone width and passes WCAG 2.2 AA automated checks and a keyboard pass
- [x] **UI-03**: The app has light and dark themes

### Documentation

- [ ] **DOC-01**: The README explains setup, architecture and eval results, and states that the project is independent and not affiliated with Elip
- [ ] **DOC-02**: A seeded demo can be run end to end in under ten minutes on a fresh machine

## v2 Requirements

Deferred. Tracked but not in the current roadmap.

### Market

- **MKT-01**: An anonymous, verified candidate brief goes out to matching employers, who request a conversation with a pay range attached
- **MKT-02**: Each employer has a visible response record (time to answer, answer rate)

### Proof

- **PRF-01**: A candidate can complete a 15 to 20 minute work sample generated from real job requirements and reuse the verified result with every employer
- **PRF-02**: Work-sample difficulty is calibrated statistically and graders are calibrated against human raters

### Outcomes

- **OUT-01**: Recorded outcomes train a learned ranker that replaces rules only when it beats them on human-labelled data
- **OUT-02**: Real offers build a pay dataset that informs negotiation

### Integrations

- **INT-01**: With permission, job-related email updates campaign status automatically
- **INT-02**: Calendar integration schedules interviews
- **INT-03**: Accepted introductions are written back to the employer's applicant tracking system
- **INT-04**: A browser extension fills application forms while the candidate watches and submits

### Coaching and Negotiation

- **COA-01**: A candidate can run a voice mock interview for a specific job
- **NEG-01**: A candidate can compare offers on multi-year value and get a drafted negotiation
- **NEG-02**: An India offer decoder shows in-hand pay and notice-period buyout

### Discovery

- **DSC-01**: The Scout agent finds companies likely to hire before they post (funding news, new leadership) and drafts an introduction note
- **DSC-02**: Warm introductions are suggested from a contact list the candidate exports and uploads

### Compliance

- **CMPL-01**: Selection rates and impact ratios are computed from logged decisions using attributes candidates volunteer for audit only
- **CMPL-02**: Counterfactual bias tests run in the eval suite

### Deferred by the design review

Not v1 requirements before; recorded here so they are not lost. Owner decisions G1 to G9 are listed in `OVERVIEW.md`.

- **DEF-01**: External agents authorize at `/mcp` through OAuth 2.1 instead of pre-issued tokens
- **DEF-02**: Company dossiers are shared across campaigns with a freshness date
- **DEF-03**: A candidate can hide individual record facts from the Twin
- **DEF-04**: The instance operator can see all users' runs and costs in the web app (G8, pending owner)
- **DEF-05**: Agents can send messages to employers other than follow-up emails
- **DEF-06**: An employer can send a serious-interest signal before the candidate accepts (G7, pending owner; revisit with MKT-01)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Unattended mass auto-apply | Few job seekers want it, major job sites forbid automation, and it feeds the application flood |
| Use of another company's code, assets, copy, branding or internal names | This is an independent implementation |
| Scraping sites behind sign-in, or people's profiles | Terms of use and ethics |
| Candidate scores from third-party data | Legal exposure; conflicts with evidence the candidate controls |
| Outcome predictions shown to employers | No data to calibrate; legal exposure |
| WhatsApp agent | Per-message cost and platform policy risk |
| Paid visibility for candidates | Conflicts with the trust model |
| Dedicated vector database, knowledge graph, multi-agent chat framework (agents that converse with each other) | Postgres and one agent runtime cover the need; LangGraph is used inside that runtime only as an orchestration library, with no hosted platform; added only on a measured trigger |

## Traceability

Which phases cover which requirements, and where each stands. Complete means the code and its tests are on `main`; a requirement that depends on a model's answers is built but unmeasured, because no live gold-set run exists (see [TESTING.md](TESTING.md)). Not on `main` means phases 7 to 9, which are not in this release.

| Requirement | Phase | Status |
|-------------|-------|--------|
| PLT-01 | Phase 1 | Complete |
| PLT-02 | Phase 1 | Complete |
| PLT-03 | Phase 1 | Complete |
| PLT-04 | Phase 1 | Complete |
| PLT-05 | Phase 1 | Complete |
| PLT-06 | Phase 1 | Complete |
| PLT-07 | Phase 1 | Complete |
| PLT-08 | Phase 1 | Complete |
| IDX-01 | Foundation slice | Complete |
| IDX-02 | Foundation slice | Complete |
| IDX-03 | Foundation slice | Complete |
| IDX-04 | Phase 2 | Complete |
| IDX-05 | Phase 2 | Complete |
| IDX-06 | Phase 2 | Complete |
| IDX-07 | Phase 2 | Complete |
| IDX-08 | Phase 2 | Complete |
| IDX-09 | Phase 2 | Complete |
| REC-01 | Phase 3 | Complete |
| REC-02 | Phase 3 | Complete |
| REC-03 | Phase 3 | Complete |
| REC-04 | Phase 3 | Complete |
| REC-05 | Phase 3 | Complete |
| REC-06 | Phase 3 | Complete |
| REC-07 | Phase 3 | Complete |
| MAT-01 | Phase 4 | Complete |
| MAT-02 | Phase 4 | Complete |
| MAT-03 | Phase 4 | Complete |
| MAT-04 | Phase 4 | Complete |
| MAT-05 | Phase 4 | Complete |
| MAT-06 | Phase 4 | Complete |
| MAT-07 | Phase 4 | Complete |
| MAT-08 | Phase 4 | Complete |
| BRF-01 | Phase 4 | Complete |
| BRF-02 | Phase 4 | Complete |
| BRF-03 | Phase 4 | Complete |
| CHT-01 | Phase 5 | Complete |
| CHT-02 | Phase 5 | Complete |
| CHT-03 | Phase 5 | Complete |
| CHT-04 | Phase 5 | Complete |
| TLR-01 | Phase 5 | Complete |
| TLR-02 | Phase 5 | Complete |
| TLR-03 | Phase 5 | Complete |
| APP-01 | Phase 5 | Complete |
| APP-02 | Phase 5 | Complete |
| HIR-01 | Phase 6 | Complete |
| HIR-02 | Phase 6 | Complete |
| HIR-03 | Phase 6 | Complete |
| HIR-04 | Phase 6 | Complete |
| HIR-05 | Phase 6 | Complete |
| HIR-06 | Phase 6 | Complete |
| HIR-07 | Phase 6 | Complete |
| HIR-08 | Phase 6 | Complete |
| CMP-01 | Phase 7 | Not on `main` |
| CMP-02 | Phase 7 | Not on `main` |
| CMP-03 | Phase 7 | Not on `main` |
| CMP-04 | Phase 7 | Not on `main` |
| CMP-05 | Phase 7 | Not on `main` |
| CMP-06 | Phase 7 | Not on `main` |
| CMP-07 | Phase 7 | Not on `main` |
| GRD-01 | Phase 7 | Not on `main` |
| GRD-02 | Phase 7 | Not on `main` |
| GRD-03 | Phase 7 | Not on `main` |
| GRD-04 | Phase 7 | Not on `main` |
| GRD-05 | Phase 8 | Not on `main` |
| GRD-06 | Phase 8 | Not on `main` |
| GRD-07 | Phase 7 | Not on `main` |
| TWN-01 | Phase 9 | Not on `main` |
| TWN-02 | Phase 9 | Not on `main` |
| TWN-03 | Phase 9 | Not on `main` |
| TWN-04 | Phase 9 | Not on `main` |
| NET-01 | Phase 9 | Not on `main` |
| NET-02 | Phase 9 | Not on `main` |
| NET-03 | Phase 9 | Not on `main` |
| EVL-01 | Phase 5 | Complete |
| EVL-02 | Phase 4 | Complete |
| EVL-03 | Phase 8 | Not on `main` |
| EVL-04 | Phase 8 | Not on `main` |
| EVL-05 | Phase 8 | Not on `main` |
| TRU-01 | Phase 3 | Complete |
| TRU-02 | Phase 3 | Complete |
| TRU-03 | Phase 9 | Not on `main` |
| TRU-04 | Phase 1 | Complete |
| TRU-05 | Phase 3 | Complete |
| UI-01 | Phase 9 | Not on `main` |
| UI-02 | Phase 9 | Not on `main` |
| UI-03 | Phase 1 | Complete |
| DOC-01 | Phase 8 | Not on `main` |
| DOC-02 | Phase 9 | Not on `main` |

**Coverage:**
- v1 requirements: 88 total (3 validated, 85 to build)
- Mapped to phases: 88 (3 to the foundation slice, 85 to Phases 1 to 9)
- Unmapped: 0
- Per phase: Phase 1: 10, Phase 2: 6, Phase 3: 10, Phase 4: 12, Phase 5: 10, Phase 6: 8, Phase 7: 12, Phase 8: 6, Phase 9: 11
- Status on `main`: 59 complete (the 3 foundation requirements and the 56 of Phases 1 to 6), 29 not on `main` (Phases 7 to 9)

---
*Requirements defined: 2026-11-22*
*Last updated: 2026-11-28: status of Phases 1 to 6 recorded*
