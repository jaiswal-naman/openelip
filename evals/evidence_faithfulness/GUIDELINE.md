# Evidence-faithfulness labelling guideline

**Status: written by an agent for agent-drafted labels; maintainer review pending.** Every pair in `gold.jsonl` says
`labels: "agent-drafted, maintainer review pending"`. A second person labels 20% of the pairs from this guideline without
looking at the first labels, agreement is reported as Cohen's kappa, and every disagreement amends this guideline
(AI_SYSTEM 12.2). Until then a report must say the labels are agent-drafted.

## What a labeller does

A pair is one candidate's record and one job posting. The labeller sees what the model sees (AI_SYSTEM 4.4):

- the candidate's **confirmed facts**, one per line, `[f1] kind: statement`. A role fact ends with its length in
  brackets and the list ends with the candidate's total career length. Both were counted by code, not by the labeller
  and not by the model: every month from the first to the last month of a role counts, and months covered by two roles
  count once. A current role ends on the date of the pair (`as_of`, 2026-11-23 for this set);
- the posting's **requirements**, one per line, `[r1] text`, each with its importance (`required` or `preferred`).

For every requirement the labeller writes one **verdict** and the facts it rests on. Nothing else is judged: not whether
the candidate should apply, not whether the posting is a good one.

## The four verdicts

| Verdict | It means | Facts cited |
|---|---|---|
| `met` | The cited facts state everything the requirement names. | one or more |
| `partly_met` | They state part of it: some of several named things, fewer years than asked, a lower degree level. | one or more |
| `not_met` | The record contains evidence against it. The one case this guideline recognises is years (below). | one or more |
| `no_evidence` | The record says nothing about it. | none |

`no_evidence` is the default. A requirement is never met because it is plausible, usual for the title or likely given
the employer. If the facts do not state it, the verdict is `no_evidence`.

## What counts as stated

1. **Use only the facts.** Do not use what you know about an employer, a school, a person or a tool. Do not read a skill
   into a job title: a "Data Engineer" role does not say Python or SQL; the skills list does.
2. **A requirement is `met` when every checkable element it names is stated.** An element is a named skill or tool, a
   degree and its field, a certification, a number of years, a scope that can be checked (a team size). Words that only
   colour it (strong, hands-on, proven, excellent, in a fast-paced environment) are not elements and are not judged.
3. **Alternatives and lists.** "Python, Go or Rust": one is enough. "Python and SQL": both are needed, one gives
   `partly_met`. A slash between two tools ("Docker/Kubernetes") means alternatives unless the sentence says both.
4. **Categories.** A fact that names a clear member of a category the requirement names ("data visualization tools" and
   a Tableau fact, "a cloud platform" and an AWS certification) supports `met`. A member of a different category does
   not.
5. **Related is not the same.** A related tool or field (Jenkins for "CircleCI", statistics for "data science") is
   `partly_met` at most, and only when the requirement is about the family ("CI/CD tools"); when it names the tool,
   the verdict is `no_evidence`.
6. **Soft skills, domain, location, work authorization, willingness to travel or relocate, language level and anything
   else the record has no field for** are `no_evidence` unless a fact states it in words. The record holds no home city
   and no work authorization (they are constraints, not facts), so a location requirement is `no_evidence`.
7. **Importance does not change the verdict.** A preferred requirement is judged exactly like a required one; importance
   only matters later, to the ranking.
8. **Degrees.** A degree at or above the level asked, in the field asked (or one the requirement calls related), is
   `met`. A lower level, or another field, is `partly_met`. No education fact at all is `no_evidence`. An MBA is a
   master's-level degree in business; "or equivalent experience" in the requirement does not change the verdict.
9. **Certifications.** The certification named is `met`; a different one is `no_evidence` (it is not a part of the one
   asked for).

## Years

A requirement that asks for N years (`5+ years`, `at least 3 years`) is judged from the lengths counted by code and from
nothing else. A number of years the candidate wrote in a summary line is a claim, not a record of work: it never makes a
years requirement `met` on its own, and the labeller does not add it to the code's numbers.

1. If the **total career length is less than N years**, the verdict is `not_met`, citing the role facts whose dates show
   the total. The record contains evidence against it: the whole career is shorter than the requirement.
2. Otherwise, take the roles that are **relevant** to the work the requirement names. A role is relevant when its title,
   its employer line or its bullets name that work, or a clear member of it (a Backend Developer role for "software
   engineering"; a Data Analyst role for "analytics"). With no named field ("7+ years of relevant experience" and
   nothing else) every role is relevant.
   - relevant roles add up to at least N years: `met`, citing those roles;
   - they add up to less than N years: `partly_met`, citing them;
   - no role is relevant: `no_evidence`.
3. Overlapping relevant roles count once. A project, an education entry or a summary line is not a role and adds nothing.

## Choosing the cited facts

Cite the fewest facts that carry the verdict, and only facts that state something about the requirement.

- A skill requirement: the skill fact (the line of the skills list), or the bullet that names the skill.
- A years or experience requirement: the role facts the years come from. For `not_met` on years, every dated role.
- A degree: the education fact. A certification: the certification fact.
- Never cite a fact because it is nearby, and never cite for `no_evidence`.

## Worked examples

All invented. `[f4]` and the other aliases are the labeller's view of one pair.

| Kind | Requirement | Facts | Verdict | Why |
|---|---|---|---|---|
| skill | "Experience with Terraform" | `[f9] skill: Terraform` | `met` | the fact names the tool |
| skill | "Experience with Python and SQL" | `[f7] skill: Python`, no SQL anywhere | `partly_met` | one of two named tools |
| skill | "Experience with Kubernetes" | no fact names it | `no_evidence` | a DevOps title does not say it |
| skill | "Experience with data visualization tools" | `[f5] skill: Tableau` | `met` | a clear member of the category |
| experience | "3+ years as a data analyst" | `[f2] role: Data Analyst at Northwind (4 years 2 months)`, career 6 years | `met` | relevant roles add up to 4 years 2 months |
| experience | "5+ years in platform engineering" | `[f1] role: DevOps Engineer (2 years 1 month)`, other roles unrelated, career 8 years | `partly_met` | relevant, but 2 years 1 month is under 5 |
| experience | "10+ years of experience" | career 4 years 8 months | `not_met` | the whole career is shorter than 10 years; cite the roles |
| experience | "Experience leading a team of engineers" | no fact says anyone was led | `no_evidence` | a manager title is not a team size |
| education | "Bachelor's degree in Computer Science or a related field" | `[f12] education: B.Sc. Computer Science` | `met` | level and field both stated |
| education | "Master's degree in Computer Science" | `[f12] education: B.Sc. Computer Science` | `partly_met` | right field, lower level |
| education | "Degree in Law" | only `B.A. Economics` | `partly_met` | a degree, but another field |
| certification | "Certified Scrum Master" | `[f20] certification: Certified Scrum Master` | `met` | named |
| certification | "AWS Certified Solutions Architect" | `[f21] certification: AWS Certified Cloud Practitioner` | `no_evidence` | a different certification |
| authorization | "Authorized to work in the United States" | no fact | `no_evidence` | not a fact kind |
| location | "Based in the Bay Area" | no fact | `no_evidence` | not a fact kind |
| other | "Comfortable in a fast-paced environment" | no fact | `no_evidence` | soft skill, colour |

## Decisions made while labelling

Twelve cases the rules above did not settle were decided while the 30 pairs were labelled. They are the place for a
second rater to start; a disagreement here amends the guideline.

1. **Leadership.** A title (Manager, Lead, Director) does not state that anyone was led. A leadership or management
   requirement is `met` only when a fact says people or a team were led, managed or mentored; otherwise `no_evidence`.
   "Mentored the release process" mentions a process, not people.
2. **Generic ability claims** (communication, ownership, problem solving, comfortable with ambiguity, organised,
   strategic thinker) are `no_evidence` even when the record lists a skill that sounds related. Where a requirement names
   the same skill the record lists in words (stakeholder management for working with stakeholders or cross-functional
   partners, technical writing for written communication), the skill supports `partly_met`: it states the named skill and
   not the rest.
3. **Systems languages** are C, C++, Rust and Go. Java, Ruby, Python, TypeScript and Kotlin are not "a comparable systems
   language".
4. **Summary lines.** A line such as "Backend Developer with 5 years of experience in cloud infrastructure" is a fact of
   kind `other`. It may support a requirement that asks for experience in that field (`partly_met` or `met`, citing it),
   never a number of years (see Years). Several of these lines claim more years than the dated roles add up to; they are
   left as the resume gold set has them.
5. **Titles and kinds.** For an `experience` requirement a role title can show relevance (a Sales Representative role for
   "managing sales cycles"), because the work the requirement names is the work the title names. For a `skill`
   requirement a title never states a skill.
6. **Qualifiers that can be checked keep a requirement from `met`** even when its headline is stated: a backend focus
   (ev-013 requirement 1), a track record of building durable systems (ev-026 requirement 2), a leading company and top
   performance (ev-020 requirement 1).
7. **Related fields.** An M.Sc. in Data Science meets "BS/MS in Computer Science or related majors" (the requirement
   asks for related majors); a B.Sc. in Computer Science is `partly_met` for "Information Systems, Data Engineering,
   Education, Statistics" (a field the requirement does not list).
8. **Tools in a category.** Grafana is a clear member of "observability toolchains" (`met`, ev-004 requirement 4) and a
   related reporting tool for "Business Intelligence experience" (`partly_met`, ev-006 requirement 5). Excel is not a BI
   or visualisation tool (`no_evidence`, ev-009 requirement 3).
9. **A language is not its frameworks, and SQL is not Postgres.** "Java frameworks such as Spring" with only Java stated,
   and "Postgres depth" with only SQL stated, are `no_evidence`: the requirement names the tool.
10. **Places, languages, travel and physical ability** (a city, fluent German, 25% travel, sitting for four hours) are
    `no_evidence`: no fact kind covers them.
11. **Unverifiable adjectives are colour.** "Graduate from a reputed institution" is `met` by a degree alone (ev-009
    requirement 6).
12. **Relevant roles for a named field** are counted by title: an Operations Manager role is relevant to "support
    operations or operations strategy" (ev-029 requirement 1, `partly_met`), a Software Engineer role to "software
    engineering" (ev-013 requirement 1).
