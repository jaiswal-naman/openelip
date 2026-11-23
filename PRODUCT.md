# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two audiences with equal weight (owner decision, 2026-11-23), each running their own copy of OpenElip on their own computer:

- **Job seekers**: one person running their own job search. They decide which jobs to pursue, read the evidence behind each match, approve what the agents prepare, and watch their campaigns.
- **Employers**: a person or small team hiring. They describe roles, read shortlists of candidates who said yes, ask questions, and answer every introduction.

One install has one owner, who can use both sides. There is no sign-in: opening the app on that computer is the identity.

## Product Purpose

An open-source, self-hosted agent hiring marketplace that does the work behind the job list and shows its evidence. It imports real postings from employers' public job boards, matches them to the person's career record, tailors resumes without inventing anything, and runs campaigns in which a crew of agents researches the company, prepares the application packet and drafts follow-ups. Success: the person spends their time deciding, not searching or typing, and can check every claim the system makes.

## Positioning

**An agent crew you supervise.** Agents research, draft and follow up; nothing leaves the system until the owner approves it (the Guardian holds every outward action; an undo window covers follow-ups; the system never submits an application on its own). Private and local underneath: the record, jobs and history stay on the owner's machine, and model calls use the owner's own API key or a local model.

## Operating Context

- Installed with one `docker compose up`; used at http://localhost:8080 in the owner's browser, desktop first, phone width supported.
- First launch: paste a model API key (or skip and explore the invented demo data), upload a resume, set hard constraints, then land on Today.
- Daily ritual: Today (decisions waiting, then what was done since last time), the morning brief by email.
- Surfaces: Today, Matches and the "Why not" view, Jobs search, Career record, Tailored resumes, Applications, Campaigns (board and per-campaign timeline, packet, dossier, crew, prep, follow-ups), Approvals and undo, Autonomy settings, Flight recorder, Costs, Eval scoreboard, Hiring desk (roles, shortlist, answers, team), Trust (what is sent where), Chat agent.

## Capabilities and Constraints

- Hard constraints are enforced in the database before any model sees a job; held-back jobs are listed with the reason.
- Every verdict, dossier sentence and resume edit cites its exact source line; missing evidence is labelled "No evidence", unknown values "Unknown" or "Not stated", never blank or guessed.
- Model features need the owner's key and a passed live evaluation for that model; until then the app says so (`ai_disabled`). No live model call has been made yet.
- Cost per campaign is metered against a budget (default USD 0.75) and shown.
- Built with Next.js 16, React 19, React Aria Components, CSS Modules with one tokens file; no UI kit dependency.
- Undecided: final public product name (OpenElip is a working name read from one constant).

## Brand Commitments

- Name shown as OpenElip (working name).
- Required footer line on every surface: "Independent open-source project. Not affiliated with Elip."
- Voice: plain, exact, honest about limits; numbers only from records; replay results are labelled wiring checks, never model quality.

## Evidence on Hand

- Invented demo data (company "Initech Demo", postings labelled "Demo posting (invented)") seeded by `python -m app.cli demo seed-campaign`.
- Real measurements in README (71 boards, 11,357 postings ingested on 22 Nov 2026; matching benchmark ndcg@5 with labels drafted by an agent, review pending).
- No testimonials, customers, users, or model-quality results exist; none may be shown.

## Product Principles

1. The person decides: the interface prepares, the owner approves, nothing outward fires from a default or a timer.
2. Evidence is one click away: every claim carries its source or an explicit "No evidence".
3. Decisions before lists: each screen opens with what needs the person.
4. Nothing happens silently: every agent step is visible in the timeline and the recorder, including blocked ones.
5. Unknown is shown as unknown.

## Accessibility & Inclusion

WCAG 2.2 AA; automated axe checks run on every screen in both light and dark themes; full keyboard operation (React Aria); reduced-motion respected.
