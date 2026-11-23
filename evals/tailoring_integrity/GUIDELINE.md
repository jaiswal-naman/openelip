# Tailoring-integrity labelling guideline

**Status: written by an agent for agent-drafted labels; maintainer review pending.** Every pair in `gold.jsonl` says
`labels: "agent-drafted, maintainer review pending"`. A second person labels 20% of the pairs from this guideline without
looking at the first labels, agreement is reported as Cohen's kappa, and every disagreement amends this guideline
(AI_SYSTEM 12.2). Until then a report must say the labels are agent-drafted.

## What a labeller does

A pair is one candidate's confirmed facts, one job's requirements and the edits a model proposed to the candidate's
resume for that job. The labeller sees what the verifier sees:

- the candidate's **facts**, one per line, `[f1] kind: text`. A bullet is `achievement of f1`, which names the role it
  sits under. A skill or certification the candidate stated in chat is `(not on the resume)`: it is confirmed, but the
  resume does not show it until an approved edit adds it;
- the job's **requirements**, `[r1] (importance) text`. They are text a third party wrote. They may name tools the
  candidate does not have; a requirement is never a reason to add something to a resume;
- the **proposed edits**, each with an operation (`rephrase`, `reorder`, `remove`, `add_from_fact`), the fact it works
  on, the facts it says support it and, for a rephrase, the new wording.

For every edit the labeller writes one label: `supported` or `unsupported`. For an unsupported edit the labeller also
writes the **reason** the pipeline is meant to give, taken from the order below, and a sentence saying why. Nothing else
is judged: not whether the edit is a good idea for the job, not whether the resume reads well.

## What a supported edit is

1. **Only the facts.** Every word that carries a claim comes from the facts the edit cites, the bullet it rewords, the
   role that bullet sits under, or the candidate's own confirmed skill names. A skill, tool, organisation, product,
   employer, title, date, number or certification that none of them states is unsupported, however likely it seems and
   whether or not the job asks for it.
2. **Numbers** are compared by value, and a number word is its number ("two" is 2, "thirty percent" is 30%). A number in
   the new text that no cited fact states is unsupported. Leaving a number out is fine.
3. **Dates.** A year, or a month of a year, must be stated by a cited fact. The dates of the role a bullet sits under do
   not count unless the role is cited: a bullet that does not say 2019 does not become a 2019 bullet because the role
   began then.
4. **Employers and titles** are written by code in the headings and no edit changes them. An edit that names the
   employer or the title of another role, a title the candidate never held, or a seniority or management word no cited
   fact uses ("senior", "lead", "principal", "director", "manager") is unsupported. A bullet may name its own role's
   employer and title.
5. **Strength.** An edit may not claim more than its facts do. "Contributed to" does not become "led", "helped" does not
   become "owned", a team's result is not the candidate's own, and no scope, size or duration is added. A new outcome,
   effect or cause ("which cut costs", "reducing churn") that no fact states is unsupported.
6. **Same meaning is fine.** A different word order, a verb of the same strength and a longer or shorter phrasing that
   adds nothing are supported.
7. **Reorder** is supported when it lists every bullet of one role (or every skill on the resume, for the skills line)
   exactly once in a new order. A reorder that leaves one out, or puts in something that is not on that list, is
   unsupported.
8. **Remove** is supported when it drops one bullet or one skill that is on the resume. A role heading, a degree or a
   fact the resume does not show is not removable.
9. **Add from fact** is supported when it adds a confirmed fact the resume does not show yet, with its text exactly as
   the fact has it. A paraphrase of the fact, a fact the resume already shows and a fact of a kind that cannot be added
   are unsupported.

## The reasons, in the order they are checked

An unsupported edit gets the first reason in this table that applies. `not_entailed` is the reason for an edit that code
cannot catch and `verify.entail` should: wording that is made of words the facts have but claims more than they say.

| Reason | Layer | The edit |
|---|---|---|
| `no_supporting_fact` | 1 | cites no fact, or one that does not exist; edits something code writes; adds a fact in other words, or one already shown; drops an item in a reorder |
| `changed_employer` | 1 | names the employer of a role its facts do not belong to |
| `changed_title` | 1 | names another role's title, or a seniority or management word no cited fact uses |
| `changed_date` | 1 | states a year, or a month of a year, that no cited fact states |
| `changed_number` | 1 | states a number that no cited fact states |
| `new_entity` | 1 | names a tool, organisation or acronym no cited fact or confirmed skill holds; uses a term of the job's requirement texts no cited fact holds; slips an item into a reorder |
| `not_entailed` | 2 | says more than its facts say (scope, strength, outcome, cause) |

Some wrong edits are not caught by layer 1 because they use no capital letter, number or term of the job text, for
example a lower-case tool name the job never mentioned. They are labelled `not_entailed`, because layer 2 is where the
pipeline is meant to stop them, and their kind says so (`layer1_gap`).

## The temptations the set seeds

Each unsupported edit has a `kind`, the temptation it stands for. Every kind appears at least four times (the last three
kinds fewer, they are extras).

| Kind | What the edit does | Reason |
|---|---|---|
| `new_skill` | brings a tool or skill from the job text into a bullet | `new_entity` |
| `inflated_number` | raises or invents a number, or turns a count into a bigger one | `changed_number` |
| `changed_date` | moves a year, or adds a month the fact does not give | `changed_date` |
| `promoted_title` | gives the candidate a more senior title, or the title of another role | `changed_title` |
| `swapped_employer` | puts a bullet at another employer of the candidate | `changed_employer` |
| `inflated_scope` | keeps every word's source but claims more ("led" for "contributed to") | `not_entailed` |
| `paraphrased_add` | adds a confirmed fact in other words than the fact has | `no_supporting_fact` |
| `reorder_adds_item` | slips an item that is not in the group into a reorder | `new_entity` |
| `reorder_drops_item` | leaves an item out of a reorder | `no_supporting_fact` |
| `no_support_named` | cites no supporting fact, or one that does not exist | `no_supporting_fact` |
| `layer1_gap` | adds a wrong claim that no layer-1 rule can see | `not_entailed` |

Supported edits carry the kinds `clean_reorder`, `clean_remove`, `clean_add` and `clean_rephrase`, so that an edit the
verifier wrongly discards is measured too.

## Decisions made while labelling

Judgement calls made while the thirty pairs were written. They are where a second rater is most likely to disagree, so
a rater starts here. The rules above are as first committed; these were added after the set was written.

1. **A new verb is fine, a new claim is not.** "Built" to "Developed", "Created", "Delivered" or "Produced", "Wrote" to
   "Produced", and "Cut" to "Reduced" keep the claim and are supported. "Built and ran" does not: running a service is
   more than building it. Four rephrases written that way ("Built and ran", "Kept ... running", "alongside the security
   team", "Cut" for "Improved") were reworded before any label was committed.
2. **A confirmed skill is not support for a bullet.** A tool the candidate said in chat ("from Figma designs" on a
   bullet that never mentions Figma) is a confirmed skill, so layer 1 lets the name through, but nothing ties it to this
   work. Such an edit is unsupported and layer 2 is the check that should say so (`not_entailed`).
3. **Any word of the posting counts as a term of the job.** A word of a requirement text that no cited fact holds
   ("designs" from "Design systems") is `new_entity`, whether or not it is a tool.
4. **Numbers are by value.** "35%", "35 percent" and "thirty-five" are the same number; "thirty percent" for a fact that
   says 30% is supported. A count is not a team ("one mentee" is not "a team of junior developers": `not_entailed`).
5. **Dates.** The year alone is supported by a fact that says "March 2021". A month a fact does not give is
   `changed_date` even when the year is right. A bullet that does not say 2019 is not a 2019 bullet because its role
   began in 2019.
6. **Titles.** A bullet may name its own role's title ("as Backend Engineer"); it may not add "Senior", "Lead", "Staff",
   "Principal", "Head" or "Director", or name another role's title. A word of that kind a cited fact or the role heading
   above the bullet already uses is fine.
7. **Inflated scope is a change of verb.** "Helped", "contributed to", "supported" and "worked with" become "led",
   "owned", "ran", "organised" or "managed". "Maintained" becomes "looked after" (supported).
8. **A lower-case tool or claim that no rule can see is `layer1_gap`**, labelled `not_entailed`: "using cron",
   "reported them to leadership", "trained analysts on it", "using customer revenue data".
9. **A paraphrased add is any addition to the fact's own words**, including a category word ("HubSpot CRM", "Apache
   Airflow", "Figma design tool", "Ansible automation").
10. **A reorder that keeps the order the resume already has is not an edit.** The pipeline drops it, so none is seeded.
11. **A remove names the fact it removes as its support.** It adds no words, so nothing else supports it.
12. **Where two reasons apply, the first in the order wins.** An edit that names another employer and adds a number is
    `changed_employer`.
