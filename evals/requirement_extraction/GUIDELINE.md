# Requirement labelling guideline

Version 1. Rules 1 to 9 and the worked example were written before any posting was read. Cases they did not settle
are listed at the end, under "Decisions made while labelling", so a reviewer can see what was added after the first
draft. The example postings in this file are invented.

Why this exists: IDX-08 stores each job's requirements as cited items (a quote, the exact place in the posting it
comes from, required or preferred). This is the written definition of a correct item that the labels in `labels.jsonl`
follow, and that the extraction score is measured against.

## What a requirement is

A requirement is a **qualification the candidate must or should already have**: a skill, experience, education, a
certification, work authorisation, or a place to be based. It is not a duty, a benefit, or a fact about the company.

Not requirements, never labelled:

- Duties and day-to-day work ("You will design services that process payments").
- Pay, benefits, perks, and the company's size, funding, offices or history.
- Application steps, equal-opportunity and legal notices, the interview process.
- Attitude or personality with nothing a résumé or interview could test ("passionate about our mission", "curious",
  "a team player", "you thrive here"). The same trait phrased as an ability someone could be asked to show ("ability
  to work independently", "comfortable presenting to executives") is a requirement of kind `skill`.

## Rules

1. **One item per distinct qualification.** The unit is a bullet (one line or list item) or, in running text, a
   sentence. A bullet that names two skills joined by "and" or commas is one item, unless the posting lists them
   separately. A line holding several sentences gives one item per sentence that states a qualification.
2. **Split a line only when the parts differ** in importance (rule 4) or each part states its own number of years
   (rule 6). Quote each clause then. Quotes never overlap.
3. **The quote** is the shortest complete bullet, sentence or clause that states the qualification, copied character
   for character from the posting: same letters, case, spacing, punctuation, quote marks and dashes. Leave out a leading
   bullet mark ("- ", "* ", a bullet character, "1." or "a)"), the line break, and any heading. Keep the final
   punctuation when the quote runs to the end of its bullet or sentence. If the same text appears more than once in the
   posting, record which one with `occurrence` (1 for the first).
4. **`importance` is `preferred` only when the posting marks it** with words such as preferred, nice to have, bonus,
   a plus, ideally, desirable, an advantage, "would be great", "extra credit", or when the bullet sits under a heading
   that says so ("Preferred qualifications", "Nice to have", "Bonus points"). Everything else is `required`, including
   soft wording under a requirements heading ("familiarity with", "exposure to").
5. **`kind`** is exactly one of:
   - `skill`: a named tool, technology, language or method, or an ability, technical or interpersonal ("Python",
     "financial modelling", "strong written communication"). "Experience with <named tool>" is a skill. Fluency in a
     spoken language is a skill.
   - `experience`: time or history of doing work: any number of years, "experience as / in / leading ...", a track
     record, experience in an industry or a kind of role.
   - `education`: a degree, diploma or field of study, with any "or equivalent experience" attached to it.
   - `certification`: a named professional certification, licence or registration (CPA, PMP, an AWS certification, bar
     admission, a nursing licence, a driving licence).
   - `authorization`: legal permission to work or access: right to work, visa status, citizenship, security
     clearance, export-control eligibility.
   - `location`: where the candidate must be based or be available: a country, city or time-zone requirement, being
     in an office on set days, willingness to relocate.
   - `other`: any other condition on the candidate: willingness to travel, physical demands, shift or weekend
     availability, minimum age, consent to a background check.
6. **`min_years`** is the smallest whole number of years of experience the quote asks for, from digits or words
   ("3+ years", "at least five years", "3-5 years" gives 3). It is null when the quote states none, or only "several",
   "a few" or "extensive", or a range that starts at zero or an upper limit only ("0-2 years", "up to 3 years"). A
   number that is not about the candidate's experience never counts.
7. **Unknown stays null.** Label only what the posting says. Never infer a requirement from the job title, the level,
   the company or what such roles usually need.
8. **Repeats.** A qualification stated twice (a summary and again in a list) is labelled once, at its first
   occurrence.
9. **No requirements at all** is a valid answer: `requirements` is an empty list.

## Worked example (invented posting)

```
About the role
You will build and run the payment pipeline for our marketplace.

What you'll do
Design services that process card payments.
Review code and mentor two junior engineers.

What we're looking for
5+ years of backend engineering experience.
Strong Python and SQL skills.
Bachelor's degree in Computer Science or equivalent practical experience.
You must be authorised to work in Canada.
Based in or willing to relocate to Toronto.
Excellent written communication.
Passionate about helping small businesses.
Experience with React is required; Vue is a plus.

Nice to have
Experience with Kafka.
AWS Certified Solutions Architect.
Fluency in French.

Benefits
Health insurance and four weeks' vacation.
```

| Quote | `importance` | `kind` | `min_years` | Why |
|---|---|---|---|---|
| 5+ years of backend engineering experience. | required | experience | 5 | years (rules 5, 6) |
| Strong Python and SQL skills. | required | skill | null | two skills on one bullet are one item (rule 1) |
| Bachelor's degree in Computer Science or equivalent practical experience. | required | education | null | the equivalent-experience clause stays inside it |
| You must be authorised to work in Canada. | required | authorization | null | |
| Based in or willing to relocate to Toronto. | required | location | null | |
| Excellent written communication. | required | skill | null | an ability |
| Experience with React is required | required | skill | null | split: the parts differ in importance (rule 2) |
| Vue is a plus. | preferred | skill | null | marked "a plus" (rule 4) |
| Experience with Kafka. | preferred | skill | null | under "Nice to have" |
| AWS Certified Solutions Architect. | preferred | certification | null | |
| Fluency in French. | preferred | skill | null | |

Not labelled: "Design services...", "Review code and mentor...", "Passionate about helping small businesses." (attitude
only), and the benefits line.

## Decisions made while labelling

These came up while labelling the 50 postings and were not settled by rules 1 to 9. Each was decided once and applied
to every posting. Ids are the postings the case occurs in.

1. **Rule 8, refined.** A qualification repeated in an introduction and in the requirements list is labelled once, in
   the list. Summary sentences that only restate listed items ("We are most interested in ...", "The ideal candidate
   will ...") are not labelled again. A summary sentence that states an ability the list does not repeat is labelled
   where it stands (req-046).
2. **Traits are not requirements, abilities are.** A bullet is labelled when it states something testable: "ability
   to", "able to", "can <verb>", "capable of", "skilled at", "comfortable / confidence <verb>-ing", skills,
   experience, knowledge, understanding, proficiency, expertise or a track record. A bullet made only of traits
   (passionate, curious, motivated, self-starter, high agency, empathy, "alignment with our values") is not labelled,
   and neither is a bare competency name with no statement ("Stakeholder Management", "Attention to Detail": req-046).
   A trait-led bullet that also states an ability is labelled (req-026, req-042).
3. **`kind` boundaries.** A named tool, technology, method, standard or report, or wording such as knowledge,
   understanding, familiarity, proficiency or "hands-on X" without the word experience, is `skill`, even when the quote
   says "experience with <the named thing>". Experience, years, a track record, a background or a past-tense verb about
   work done ("Performed", "Led") is `experience`. A bullet that mixes kinds takes the kind of its first clause. A short
   label sentence followed by its elaboration ("A strong financial foundation. You've owned ...") is one item and takes
   the kind of the elaboration.
4. **Importance wording.** "Is a plus", "bonus", "helpful but not required", "strongly preferred", "an advantage",
   "highly desirable" and a single "(Preferred)" inside a bullet make it `preferred`. A "Desired" list that follows a
   "Required" list is preferred (req-024). "Experience we value", as a second list after the main one, is preferred
   (req-027). "Desired Qualifications" as the posting's only qualification list is required (req-001, req-049). An
   "ideally / preferably / preferred" clause that only narrows the same qualification stays inside one required item
   (req-003, 005, 010, 014, 023, 026, 028, 030, 046, 050).
5. **Splitting.** Split by importance when the parts are separate qualifications: req-011, 018, 022, 031, 035, 036,
   039, 045. Split by years: req-014 ("10+ years ... including 5+ years leading managers"), req-033. Two alternatives
   where the second only restates the first are one item (req-022, "Postgres depth ... OR Love of Postgres").
6. **Years.** "More than N years" is N (req-004, 032, 043). A range gives its lower bound ("3-6 years" is 3, "8–10" is
   8, "At least 1 - 2 year" is 1). A fractional number cannot be a whole number, so it is null (req-012, "1.5 years").
7. **Quoting.** A bullet wrapped over a hard line break is one item and its quote contains the line break (req-001,
   req-049). A numbered-list marker is left out of the quote ("1." in req-032, 043). A marker word at the start of a
   bullet stays in ("Bonus : ...", "MUST HAVE: ..."). A trailing explanation after a dash ("- essential to ...") is
   left out (req-010). Items run together in one paragraph with no separators are split at their clause boundaries
   (req-010, req-050).
8. **Location.** "This role is based in <place>", "remote-first, anywhere in <region>", a "Location:" header, a city
   line and an in-office-days expectation are `location`, required (req-006, 007, 011, 013, 014, 016, 018, 019, 021,
   024, 027, 029, 030, 036, 040, 041, 042, 045, 048). A statement of two sentences gives two items (req-029, req-030).
   Facts about the employer's offices are not labelled.
9. **`other`.** Willingness to travel, physical demands, shifts and a start date (req-012, 013, 024, 029, 035, 038),
   and a portfolio, public work or a contact network the candidate must show (req-006, 008, 019). A bullet that joins a
   location with travel is one `location` item (req-019).
10. **Not labelled.** A list headed "Requirements" that holds only duties ("Expectations/ Requirements" in req-032 and
    req-043), the "Durable Skills" values list (req-009), a "Success Profile" of behaviours (req-020), a duty line
    "Travel ... as required" under Responsibilities, a company's visa-sponsorship policy (req-040), and a second sentence
    that only restates the first (req-016).
