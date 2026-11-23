# Matching-benchmark labelling guideline

**Status: written by an agent for agent-drafted labels; maintainer review pending.** Every label in `labels.jsonl` says
`labels: "agent-drafted, maintainer review pending"`. A second person labels a random 20% of the pairs from this
guideline without looking at the first labels, agreement is reported as Cohen's kappa (quadratic-weighted: the grades are
ordered), and every disagreement amends this guideline (AI_SYSTEM 12.2). Until then a report must say the labels are
agent-drafted. Written on 2026-11-23, before any run (see `README.md`).

## What a labeller does

A pair is one invented candidate profile and one real public job posting. The labeller sees the profile and a compact
card of the posting, and gives one grade for how relevant the posting is to that candidate this morning. Nothing else is
judged: not whether the posting is a good job, not whether the company is good, and not how the posting was found.

**The question to ask:** if a careful career adviser who knew this person's record and wishes saw this posting, how
strongly would they put it in front of the person?

## The grades

| Grade | Name | It means |
|---|---|---|
| 3 | Would apply now | The role is the one the person is looking for or a very close one, at about their level, and their record states most of what the posting asks. Nothing in the card is a reason to hesitate. |
| 2 | Relevant | The right field and role family at a plausible level. The person would read it with interest and might apply: some named skills are missing from the record, or one of their wishes is not met, or the level is one step off. |
| 1 | Marginal | Adjacent: a related field, a stretch in level (two or more steps), or only a minor part of the record fits. The person would not search for it but would not be surprised to see it. |
| 0 | Not relevant | Another field or function, or clearly the wrong level, or nothing of the record fits. |

Grades are about relevance, not about retrieval: a posting is never graded up because its words match the profile's
words, and never graded down because it is unusual.

## How to read a profile

The card for a profile lists, in this order:

1. **Facts:** roles with their dates (employer, title, start, end or "present"), skills, education. Only what is written
   counts; do not add skills a title usually implies (a "Data Engineer" role does not state Python unless a skill line
   does).
2. **Looking for** (preferences, with a weight from -3 to 3): target titles, skills the person wants to use, industries, a
   place or an arrangement they prefer, a level. A negative weight is something they would rather avoid. A preference is
   a wish, not a rule: it moves a grade by at most one step.
3. **Hard constraints:** places, arrangements, an excluded employer, a pay floor, work-authorization countries. **The
   matching code has already applied these**: every pooled posting passed the filter. Do not re-check them and do not
   grade a posting down because of one. A posting that leaves a constrained field unstated passed the filter and is
   *unverified*; grade it on the role and note "unverified" if it matters.

## How to read a posting card

A card shows the title, the company, the place the posting states (country and city, or "not stated"), the arrangements
it states (onsite, hybrid, remote, or "not stated"), the level it states ("not stated" is the usual case, because the
index stores no seniority), pay if stated (usually not), and the first 600 characters of the description.

- Judge the **role**: what the person would do each day. The title is the strongest signal but not the only one: read the
  first lines for what the team does, what is asked and at what level.
- Level: judge it from the title and the lines (a "Senior", "Staff", "Lead", "Principal", "Head of" or "Director" title, or
  "5+ years", "8+ years"). One step is one rung on intern, entry, mid, senior, staff or principal, manager, director. The
  person's level is read from their most recent roles and their dates.
- Do not use anything outside the card and the profile: not what you know about the company, not the rest of the
  description, not how many other postings look alike.
- **Do not look at how the posting was found.** The cards are printed in an order that hides it; do not try to recover it.
  Judge each pair on its own, so that the same posting for two candidates can get two grades.

## Rules of thumb

1. **Same title is not enough.** A "Software Engineer" posting that asks for ten years of embedded firmware is not
   relevant to a web developer with three years. Field and level both count.
2. **Field first, then level, then wishes.** A different field is 0 or 1 whatever the level. Within the right field, one
   step of level costs one grade from 3, two or more steps cost two grades. A wish not met costs at most one grade.
3. **Several roles in one profile.** Grade against the best-fitting direction the person's record and wishes point to; a
   candidate who wants to move from analytics to data engineering finds both relevant if the record states the skills.
4. **Skills:** a posting that names mostly skills the record states is a 3 or 2; one that names skills the record does not
   state at all is a 1 at best, even in the right title.
5. **Internships, contracts, part time:** judge the role as above; the employment type is a hard constraint if the person
   set one (already applied) and is otherwise ignored.
6. **Unstated everything:** a very short or empty card is graded on the title alone and kept at 2 or below.
7. **Postings in another language:** read what you can; if the role cannot be judged, grade 0 and say "cannot read" in the
   note.
8. **A posting that repeats an earlier pair** (the same role at the same company in another city): grade it as the other,
   unless the card says something different.
9. **Notes** are at most 120 characters: the reason in plain words ("same field, two levels senior", "skills match, wants
   remote, onsite only").

## Worked examples

These use invented profiles and invented postings. They show the reasoning, not a gold answer.

**Profile A (Demo Candidate, 5 years):** backend developer 2021 to present, Python, Django, PostgreSQL, REST APIs, AWS;
earlier QA engineer 2019 to 2021. Looking for: "backend engineer", "python" (+2), remote (+1).

| Posting card | Grade | Reason |
|---|---|---|
| Backend Engineer, Python, remote. Build APIs with Django and PostgreSQL on AWS. 3+ years. | 3 | The role, the level and nearly every named skill. |
| Senior Backend Engineer (Python), hybrid in Berlin. Lead a small squad; 6+ years. | 2 | Right field, one step senior, wants remote but hybrid. |
| Backend Engineer, Java and Spring, remote. 4+ years of Java. | 1 | Right role and level, but the named stack is one the record does not state. |
| Staff Engineer, distributed systems, Go. 10+ years, tech lead for 15 engineers. | 0 | Field close, level three steps up, skills not stated. |
| Technical Recruiter, engineering. Source and screen candidates. | 0 | Another function. |

**Profile B:** marketing coordinator 2020 to 2023, marketing manager 2023 to present; email campaigns, SEO, content
planning, HubSpot. Looking for: "marketing manager" (+3), B2B software (+1).

| Posting card | Grade | Reason |
|---|---|---|
| Marketing Manager, B2B SaaS. Own lifecycle email and content; HubSpot. 4+ years. | 3 | The title, the level and the tools. |
| Content Marketing Specialist, fintech. SEO articles and newsletters. | 2 | Same field, one step junior, skills match. |
| Product Marketing Manager, developer tools. Positioning and launches; technical background preferred. | 1 | Adjacent function; the record states nothing about launches or developer audiences. |
| Account Executive, SaaS. Quota-carrying sales. | 0 | Another function. |

**Two grades for one posting.** "Data Analyst, SQL and dashboards, 2+ years" is a 3 for a candidate with two years of
analytics and SQL who wants an analyst role, and a 1 for a senior data engineer whose record shows pipelines and
warehouse work at a level three steps above it. The same posting, two grades, as the question is about the person.

## Decisions made while labelling

Judgement calls that came up while the 366 pooled pairs were graded, written down so a second rater starts with them.
Each is a rule of this guideline applied to a case it did not spell out. They were fixed during the first and only
pass over the pairs (profile `p01` to `p20`, in that order, each profile's cards in the hidden order) and no grade was
changed after a metric had been computed.

1. **The card's 600 characters are mostly company boilerplate.** The first lines of most postings describe the employer,
   not the role, so most grades rest on the title, the place and the arrangement the card shows. A grade of 3 therefore
   means "the title, level and field the profile asks for and nothing visible against it", not "the requirements were
   checked".
2. **Level is read from the title.** From a title that names no level, the level is taken as the profile's own. One step
   from the profile's current level (senior to staff, manager to senior manager, a junior title for a senior person)
   costs one grade from 3; two or more steps (senior staff, director, vice president, or the reverse) cost two. An
   internship or new-graduate posting for an experienced person is 0, and so is an experienced role for a student.
3. **A generic title in the profile's field** ("Senior Software Engineer" with no stack) is 2 when nothing on the card
   points at the profile's specialty, and 3 when the title names it ("Backend Engineer", "Site Reliability Engineer").
4. **The same field on the other side of the customer** (applied AI, forward deployed or solutions roles for an engineer;
   an engineer's role for a solutions architect) is 1, unless the profile lists that role among its wishes: then it is
   graded as the wish (2 or 3).
5. **Management and individual contribution** are different roles: an individual-contributor posting for a profile that
   wants to manage is 0, and a management posting for an engineer is 1 in the same field and 0 in another. This is
   graded even where the profile has a "manager or director level only" constraint (`p06`): the card states no level, so
   the filter lets the posting through, and the wish ("engineering manager") decides the grade.
6. **A language, region or market the record does not state** ("German speaker", "Korean market", "Spanish-speaking") costs
   one grade: the records hold no languages and no markets.
7. **A segment below the record's** (commercial or small-business sales for an enterprise seller) costs one grade, and
   another market costs a further one.
8. **A talent-community entry or a fixed-term contract** costs one grade from what the role would otherwise earn: it is
   not the open, permanent job the profile looks for.
9. **A function next door** (partner sales for a marketer, key-account sales for a customer success manager, research for an
   engineer) is 1 and a different function is 0.
10. **The same posting for two profiles** gets two grades (`Applied AI Engineer, Codex` is 2 for a machine-learning
    engineer who asks for applied AI roles, 1 for a full-stack engineer, 0 for an engineering manager and for a student).
