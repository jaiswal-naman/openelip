# Roadmap: OpenElip

## Overview

OpenElip starts from the foundation slice that already works (a live job index over public Greenhouse, Lever and Ashby boards with keyword search) and grows into a two-sided agent hiring marketplace in nine phases. Phases 1 to 6 build the core agent-marketplace loop on both sides, on an evidence-first base: platform, job index, Career Record, matches with a morning brief, chat and tailoring, and the hiring desk. Phases 7 to 9 add the extensions: done-for-you campaigns under the Guardian, mission control with the wind tunnel, and the Twin with the agent network, ending in a seeded demo a stranger can run in under ten minutes. Every phase ends in something a person can run and see, so the project can be shown at any phase boundary.

The foundation slice (IDX-01, IDX-02, IDX-03) is complete and is not a phase here. Phase 1 moves that code into `backend/app/` and keeps its tests passing.

## How evaluation is spread across phases

- Phase 1 builds the eval harness skeleton (`eval_runs`, `eval_items`, a runner under `evals/`) and the CI regression gate (PLT-08).
- Each AI feature ships its gold set in the same phase as the feature: requirement extraction (Phase 2), resume extraction (Phase 3), evidence faithfulness and the matching benchmark (Phase 4), chat citations, the injection suite and tailoring integrity (Phase 5), role intake (Phase 6), dossier and Coach (Phase 7), Twin (Phase 9).
- EVL-01 maps to Phase 5 because that is where the last of its four named gold sets (tailoring integrity) lands and the requirement becomes fully true. The other three ship earlier, in their feature phases.
- The wind tunnel (EVL-03, EVL-04) runs in Phase 8, straight after campaigns exist. Phase 9 adds a Twin scenario to it.
- Every quality number shown before Phase 8 (location accuracy, the matching benchmark) comes from a recorded eval run. Phase 8 adds the check that enforces this everywhere (EVL-05); figures read from the ledgers are labelled as measured operational data.

## Cross-cutting rules

- Export and deletion land in Phase 3 with a registry test, so every later table holding user data must register a deleter or CI fails.
- Automated accessibility checks run in CI from Phase 1 on each screen as it is added. Phase 9 does the full keyboard and phone-width sweep (UI-02).
- Light and dark themes are set in Phase 1 (UI-03) so every later screen inherits them.
- An "outside action" is something an agent does that reaches anyone other than the candidate. v1 has three types: `submit_application` (prepared only; the candidate submits), `send_follow_up` (Phase 7) and `twin_answer` (Phase 9). The candidate's own clicks (accepting an introduction, sending a signal, replying to a Twin question) are not agent actions; they are recorded in the audit log. No agent takes an outside action before Phase 7, and Phase 7 builds the policy engine, the Guardian and the undo window before any send path is wired.
- Names, enums and statuses follow `DATA_MODEL.md` section 1.3, as agreed in the design review. Where an owner decision (G1 to G9 in `OVERVIEW.md`) is still open, plans assume the recommended default.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Platform and web shell** - One-command stack, sign-in, costed model gateway, queue, CI with eval gate, and a public jobs page
- [x] **Phase 2: Searchable job index** - Search by meaning, source and freshness on every job, scheduled refresh, requirements extracted with spans
- [ ] **Phase 3: Career Record** - AI-processing consent, resume to confirmed facts with source lines, typed constraints, change log, export and deletion
- [ ] **Phase 4: Matches and morning brief** - At most five constraint-safe matches a day with cited evidence tables, "why not", and a daily brief
- [ ] **Phase 5: Chat, tailoring and applications** - Cited chat with propose-and-confirm, resume tailoring that cannot invent, packets and a tracker
- [ ] **Phase 6: Hiring desk** - Roles, anonymous preview, consent-first introductions, said-yes shortlist, answer clock, interest signals
- [ ] **Phase 7: Campaigns under the Guardian** - Durable done-for-you campaigns run by the agent crew, gated by autonomy settings, two keys, an undo window, and budget and scam pauses
- [ ] **Phase 8: Mission control and wind tunnel** - Live agent status, cost meter, simulated market with five-of-five scenarios, scoreboard, measured README
- [ ] **Phase 9: Twin, agent network and release** - Cited Twin answers inside consent scopes, MCP server, decision audit log, full app, ten-minute demo

## Phase Details

### Phase 1: Platform and web shell
**Goal**: A developer can start the whole product with one command, sign in, and search the existing job index in the web app, on a platform where every model call is costed and every pull request passes an eval gate
**Mode:** mvp
**Depends on**: Nothing (first phase; builds on the foundation slice)
**Requirements**: PLT-01, PLT-02, PLT-03, PLT-04, PLT-05, PLT-06, PLT-07, PLT-08, UI-03, TRU-04
**Success Criteria** (what must be TRUE):
  1. On a fresh clone, `docker compose up` starts the database, API, web app and a local mail inbox; `/jobs` searches the existing index by keyword with country and remote filters in a light or dark theme; the foundation tests still pass after the code moves to `backend/app/`
  2. A user signs in with a one-time code read from the local mail inbox (stored only as a keyed HMAC in `login_codes`), holds a session cookie set through the single reverse proxy, stays signed in after a page refresh, and can sign out; a write repeated with the same idempotency key returns the first response and changes nothing
  3. A model call through the gateway is rejected when its structured output is invalid and is recorded with task, model, tokens, cost and latency, visible at `/control/costs`; moving a task between Anthropic, a second provider and a local OpenAI-compatible endpoint is a configuration change only; `/trust` lists every model provider and sub-processor in use
  4. A failing background job retries and then lands in the dead-letter state, a job with a duplicate unique key is not queued twice, and a scheduled job fires on time; a test fails if any log line or trace contains resume, chat or contact text
  5. Every pull request runs lint, type checks, tests, a migration check and the eval regression gate, and a deliberately degraded gold-set fixture makes the gate fail
**Demo**: Run `docker compose up`, sign in with the code from the local inbox, search `/jobs`, switch theme, and open `/control/costs` to see a test model call priced.
**Research flag**: Postgres-backed queue library versus a hand-written queue; current provider SDKs; how CI runs gold sets without a paid key (replay recorded responses, call live only when a prompt or model changes); whether the reverse proxy streams server-sent events without buffering.
**Plans**: TBD
**UI hint**: yes

### Phase 2: Searchable job index
**Goal**: Anyone can search the live index by meaning as well as keywords, see where each job came from and when it was last checked, and see each job's requirements as cited items ready for matching
**Mode:** mvp
**Depends on**: Phase 1
**Requirements**: IDX-04, IDX-05, IDX-06, IDX-07, IDX-08, IDX-09
**Success Criteria** (what must be TRUE):
  1. On `/jobs`, a plain-English query returns relevant jobs that share no keyword with it, keyword and vector rankings are fused into one list, country and remote filters still apply, and all of it works with no model API key
  2. Every job card and job page shows its source board and when it was last verified open
  3. A recorded eval run on a labelled sample shows country and city recognised for at least 95% of postings, and a posting whose location cannot be read shows "unknown", never a guess
  4. The index refreshes on a schedule with nobody running a command; the re-check that runs before a job is surfaced closes a job whose apply link has died; an operator bulk-imports candidate board names and only boards that answer with live postings are registered
  5. A job page lists its requirements as discrete items marked required or preferred, each highlighting its span in the posting; an unchanged job is not extracted again; the requirement-extraction gold set runs in the CI eval gate
**Demo**: Search `/jobs` for a plain-English query, open a result, and see its source board, last-verified time and highlighted requirement list.
**Research flag**: Local embedding library and a 384-dimension model.
**Plans**: TBD
**UI hint**: yes

### Phase 3: Career Record
**Goal**: A candidate agrees to AI processing, turns a resume and a short intake into a record of confirmed facts with source lines, typed hard constraints and soft preferences, with every change logged, and can export or delete all of it
**Mode:** mvp
**Depends on**: Phase 1 (independent of Phase 2; the two can be built in either order)
**Requirements**: REC-01, REC-02, REC-03, REC-04, REC-05, REC-06, REC-07, TRU-01, TRU-02, TRU-05
**Success Criteria** (what must be TRUE):
  1. AI features run only after the candidate agrees to AI processing with the providers named; without that consent, search and the record still work and each AI feature says why it is off
  2. A candidate uploads a PDF or DOCX resume and sees extracted facts, each showing the resume line it came from; hidden or off-page text is stripped and flagged to them; the candidate confirms, corrects or rejects each fact, and a test shows that unconfirmed and rejected facts are used nowhere; the resume-extraction gold set runs in the CI eval gate
  3. A short guided intake captures and confirms hard constraints (location, remote policy, work authorization, salary floor, employment type, seniority, excluded companies) and soft preferences; a dealbreaker that cannot be typed is saved as a soft preference with a visible "not guaranteed" notice
  4. `/app/record` shows the whole record on one page, any field can be edited, and every change appears in a log with before, after, source and time; a protected characteristic given in any input is not stored in the Career Record, proven by a test (recommended answer to owner decision G4)
  5. The candidate downloads a machine-readable archive of all their data, and after they delete their account no row, file or embedding about them remains; a registry test fails CI if any table holding user data has no deleter
**Demo**: Agree to AI processing, upload a sample resume, click a fact to see its source line, complete the intake, edit a constraint and watch the change log, then download the export.
**Research flag**: PDF and DOCX text extraction libraries, including detection of hidden and off-page text.
**Plans**: TBD
**UI hint**: yes

### Phase 4: Matches and morning brief
**Goal**: A candidate gets at most five constraint-safe matches a day, each with a requirement-by-requirement evidence table they can check line by line, a "why not" view of everything held back, and a morning brief by email and in the app
**Mode:** mvp
**Depends on**: Phase 2, Phase 3
**Requirements**: MAT-01, MAT-02, MAT-03, MAT-04, MAT-05, MAT-06, MAT-07, MAT-08, BRF-01, BRF-02, BRF-03, EVL-02
**Success Criteria** (what must be TRUE):
  1. No job that violates a stated hard constraint is ever shown: a property test over at least 10,000 generated cases passes and an independent checker runs on every match run; `/app/today` shows at most five ranked matches, and shows none when nothing qualifies
  2. Each match shows a requirement table with a verdict (met, partly met, not met, no evidence), the quoted job text and the cited record fact; selecting a row highlights the exact job sentence and resume line; the database rejects a verdict whose citation does not resolve and that job is withheld; the evidence-faithfulness gold set runs in the CI eval gate
  3. "Why not" lists withheld and filtered jobs with reason codes; a job is never shown twice unless it materially changed, and then the change is named; the candidate saves or dismisses a match with a reason, and a dismissal reason can propose a record change that takes effect only when confirmed
  4. One brief a day arrives by email and in the app by 07:00 local time and is never sent twice, even when the brief job runs twice; it lists the decisions waiting on the candidate and what was done since the last brief, never includes a job that failed its apply-link re-check, and can be set to daily, weekly or paused, or unsubscribed in one click
  5. A recorded benchmark run over labelled resume and job pairs compares at least four matching methods on quality and cost, and its report is in the repository
**Demo**: Open the seeded candidate's morning brief in the local inbox, click through to a match, select a requirement row to see both highlights, then open "why not".
**Plans**: TBD
**UI hint**: yes

### Phase 5: Chat, tailoring and applications
**Goal**: A candidate can talk to the agent about their record and matches, get a tailored resume that cannot invent experience, and leave with a ready-to-submit application packet and a tracker
**Mode:** mvp
**Depends on**: Phase 4
**Requirements**: CHT-01, CHT-02, CHT-03, CHT-04, TLR-01, TLR-02, TLR-03, APP-01, APP-02, EVL-01
**Success Criteria** (what must be TRUE):
  1. The candidate asks about their record, a match or any decision and the answer cites ledger entries; asked to search, the agent applies the candidate's hard constraints without being told; a chat gold set checks that answers carry citations
  2. A record change the agent proposes appears as a proposal and nothing changes until the candidate confirms; an injection suite with instructions hidden in job text and resumes passes with no instruction followed
  3. A tailored-resume request shows proposed edits as a per-edit diff; edits that add unsupported skills, numbers, dates, titles or employers are discarded before the candidate sees them and the number discarded is shown; the candidate approves or rejects each edit and exports PDF and DOCX
  4. Each job gets an application packet (tailored resume, answers to common questions, a short note) for the candidate to submit themselves, and the candidate tracks each application as applied, interview, offer, rejected or no response, with timestamps
  5. Gold sets for resume extraction, requirement extraction, evidence faithfulness and tailoring integrity all run in CI on every prompt or model change, and a change that drops a score below its baseline fails the pull request
**Demo**: Ask the agent why a job was ruled out, accept a record change it proposes, tailor a resume and see the "edits discarded" count, then download the packet as PDF.
**Plans**: TBD
**UI hint**: yes

### Phase 6: Hiring desk
**Goal**: An employer describes a role and gets a shortlist of only the candidates who said yes, each with an evidence table, and must answer every introduction on a visible clock
**Mode:** mvp
**Depends on**: Phase 4 (independent of Phase 5; the two can be built in either order). Needs nothing from Phase 7, because accepting is the candidate's own action, not an agent action
**Requirements**: HIR-01, HIR-02, HIR-03, HIR-04, HIR-05, HIR-06, HIR-07, HIR-08
**Success Criteria** (what must be TRUE):
  1. An employer creates an organization and invites members, and a row-level security test proves one organization cannot read another's data
  2. An employer describes a role in text and confirms the extracted requirements, with a role-intake gold set in the CI eval gate; a role without a pay range cannot go live; before anyone accepts, the employer sees only an anonymous count of matching candidates
  3. A matching candidate is asked privately, in the app and as an introduction item in their brief, sees exactly what the employer would receive before accepting, and silence for seven days counts as no; accepting releases that frozen preview in one step with no undo; a candidate who blocked an employer in advance is never offered to it; no introduction request is sent twice
  4. `/hire/shortlist` holds only candidates who accepted that role, each with the requirement evidence table; the employer answers each introduction yes or no with a reason within five business days, `/hire/answers` shows what is owed, and an overdue answer pauses new introductions
  5. Each side can send a limited number of "serious interest" signals per week, the other side sees them, and the weekly limit is enforced
**Demo**: As a seeded employer, post a role and watch the anonymous count, switch to the candidate to accept the request, then return to see the said-yes shortlist with evidence and the answer clock running.
**Plans**: TBD
**UI hint**: yes

### Phase 7: Campaigns under the Guardian
**Goal**: Approving a match or accepting an introduction starts a done-for-you campaign in which agents research, prepare, chase and coach, and no outside action happens unless the candidate's autonomy setting, the policy engine and the Guardian all allow it
**Mode:** mvp
**Depends on**: Phase 5 (packets and tracker), Phase 6 (campaigns started by an accepted introduction)
**Requirements**: CMP-01, CMP-02, CMP-03, CMP-04, CMP-05, CMP-06, CMP-07, GRD-01, GRD-02, GRD-03, GRD-04, GRD-07
**Success Criteria** (what must be TRUE):
  1. Approving a match (entering at researching) or accepting an introduction (entering at applied) starts a campaign on `/app/campaigns` that moves through researching, needs you, applied, talking, interview, offer and closed; the candidate can pause or cancel it at any moment; the campaign runs as a LangGraph graph (one thread per campaign) with a PostgreSQL checkpointer, resumed and woken only by queue jobs; a kill-and-resume test (process killed mid-node, clock moved forward by days) shows no lost step and no repeated effect
  2. Each campaign gets a one-page company dossier whose every claim links to a fetched public source, a packet from the Writer and Operator that the candidate approves in one action, and interview preparation from the Coach built on the candidate's evidence gaps and the dossier; dossier and Coach outputs have gold sets in the CI eval gate
  3. In settings the candidate sets ask-first, tell-me-after or never per outside action type where that type allows it; follow-ups and applications never proceed without approval, and no agent can share a profile; an outside action runs only when the policy engine and the Guardian both pass it, when cleared and again when it runs, and a blocked action shows in the campaign with its reason
  4. After the configured number of quiet days the Chaser drafts a follow-up that is not sent until the candidate approves it and goes only to an address the candidate entered; an approved follow-up email waits in a ten-minute undo window (configurable) during which the candidate can recall it, and it is never sent twice
  5. Every agent step is in the flight recorder with its inputs, cited evidence, cost and outcome and can be replayed step by step in the app; the Guardian pauses a campaign that reaches its model budget or shows signs of a scam posting and records the reason, and the candidate can raise the budget and resume, or cancel
**Demo**: Approve a match, watch the board move from researching to needs you, read the cited dossier, approve the packet in one click, then approve a follow-up and undo it inside the window.
**Build order inside the phase**: policy engine, Guardian checks, flight recorder and undo window come before any send path is wired.
**Adopted design** (owner decision, 2026-11-22): campaigns are a LangGraph graph with a PostgreSQL checkpointer, one thread per campaign; queue jobs drive resume and timers; a node killed mid-run resumes with no repeated effect, because unique keys in our tables guard every effect (`ARCHITECTURE.md` section 5.9).
**Plans**: TBD
**UI hint**: yes

### Phase 8: Mission control and wind tunnel
**Goal**: A candidate can watch their agents live and see what their campaigns cost, and agent behaviour is proven in a simulated job market, with every published quality or performance number traced to a recorded eval run
**Mode:** mvp
**Depends on**: Phase 7
**Requirements**: GRD-05, GRD-06, EVL-03, EVL-04, EVL-05, DOC-01
**Success Criteria** (what must be TRUE):
  1. `/control/runs` shows the signed-in candidate's own agents and their live status for each campaign as it changes, and the candidate sees model cost per campaign and per month
  2. The wind tunnel runs whole campaigns against scripted employer personas (silent, lowball, scam, hidden instruction, and a cooperative control) and reports outcomes and every invariant violation
  3. Each scenario runs five times and passes only if all five pass, and the results appear on the `/control/evals` scoreboard, readable by any signed-in user, with their run ids
  4. A check fails CI when a quality or performance number in the app or the README does not trace to a recorded eval run, and figures read from the ledgers are labelled as measured operational data
  5. The README explains setup, architecture and eval results (pulled from recorded runs) and states that the project is independent and not affiliated with Elip
**Demo**: Start a wind tunnel run, watch the agents' live status in mission control, then open the scoreboard to see each persona's five-of-five result.
**Plans**: TBD
**UI hint**: yes

### Phase 9: Twin, agent network and release
**Goal**: After an introduction, employers and outside AI agents can question the candidate's Twin and act on matches only inside the candidate's consent, the candidate can audit every automated decision, and a stranger can run the whole seeded demo in under ten minutes
**Mode:** mvp
**Depends on**: Phase 6 (introductions), Phase 7 (Guardian), Phase 8 (wind tunnel and scoreboard)
**Requirements**: TWN-01, TWN-02, TWN-03, TWN-04, NET-01, NET-02, NET-03, TRU-03, UI-01, UI-02, DOC-02
**Success Criteria** (what must be TRUE):
  1. After a candidate accepts an introduction, the employer asks the Twin and gets answers that cite record facts; when the record lacks the answer the Twin says so, asks the candidate, and relays the reply only after the candidate gives it; the candidate reads every question and answer at `/app/twin`
  2. The candidate sets which topics the Twin may answer and which never; current pay and contact details are never answered, shown by a Twin gold set in the CI eval gate and a wind-tunnel scam persona that fails to extract them in five of five runs
  3. The candidate's own agent connects to `/mcp` with a candidate token and searches matches and reads explanations; an employer's agent with an organization token asks the Twin inside an accepted introduction and requests introductions for an open role, each of which takes effect only when the candidate accepts; a call outside a token's scopes is refused and logged
  4. The candidate reads an audit log of every automated decision about them: matches shown and withheld, Guardian blocks, Twin answers and agent-network calls
  5. The candidate app (Today, Campaigns, Record, Twin, Settings), hiring desk (Roles, Shortlist, Conversations, Answers) and mission control (Runs, Recorder, Evals, Costs) are complete; every screen passes WCAG 2.2 AA automated checks and a keyboard pass at phone width; on a fresh machine the seeded demo runs end to end in under ten minutes
**Demo**: With the seed setting the candidate's Twin dial to Tell me after and opening the record topics, connect an MCP client with an organization token, ask the Twin one question it answers directly, one it must check with the candidate and one it must refuse, then run the seeded demo from a fresh clone against a stopwatch.
**Research flag**: Current MCP Python SDK and its HTTP transport.
**Plans**: TBD
**UI hint**: yes

## Coverage

| Phase | Requirements | Count |
|-------|--------------|-------|
| Foundation slice (complete) | IDX-01, IDX-02, IDX-03 | 3 |
| 1. Platform and web shell | PLT-01 to PLT-08, UI-03, TRU-04 | 10 |
| 2. Searchable job index | IDX-04 to IDX-09 | 6 |
| 3. Career Record | REC-01 to REC-07, TRU-01, TRU-02, TRU-05 | 10 |
| 4. Matches and morning brief | MAT-01 to MAT-08, BRF-01 to BRF-03, EVL-02 | 12 |
| 5. Chat, tailoring and applications | CHT-01 to CHT-04, TLR-01 to TLR-03, APP-01, APP-02, EVL-01 | 10 |
| 6. Hiring desk | HIR-01 to HIR-08 | 8 |
| 7. Campaigns under the Guardian | CMP-01 to CMP-07, GRD-01 to GRD-04, GRD-07 | 12 |
| 8. Mission control and wind tunnel | GRD-05, GRD-06, EVL-03, EVL-04, EVL-05, DOC-01 | 6 |
| 9. Twin, agent network and release | TWN-01 to TWN-04, NET-01 to NET-03, TRU-03, UI-01, UI-02, DOC-02 | 11 |

Mapped: 88 of 88 v1 requirements (3 complete, 85 across Phases 1 to 9). No orphans, no duplicates.

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9. Phases 2 and 3 do not depend on each other, nor do Phases 5 and 6.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Platform and web shell | 7/7 | Complete | 2026-11-23 |
| 2. Searchable job index | 8/8 | Complete | 2026-11-23 |
| 3. Career Record | 0/TBD | Not started | - |
| 4. Matches and morning brief | 0/TBD | Not started | - |
| 5. Chat, tailoring and applications | 0/TBD | Not started | - |
| 6. Hiring desk | 0/TBD | Not started | - |
| 7. Campaigns under the Guardian | 0/TBD | Not started | - |
| 8. Mission control and wind tunnel | 0/TBD | Not started | - |
| 9. Twin, agent network and release | 0/TBD | Not started | - |
