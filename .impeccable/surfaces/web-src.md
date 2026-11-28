---
version: 1
slug: "web-src"
primary_target: "web/src"
related_targets: []
---

# Web app surface brief

Scope: the whole signed-in web app in `web/src` (candidate side, hiring desk, mission control, settings, setup) plus the public pages it shares a shell with. Mode: **Operate**. Audience: job seekers and employers, equal weight, each on their own local install. Job: decide what the agent crew prepared, read the evidence, approve or refuse outward actions. Constraints: React Aria behaviour and accessibility kept; every claim keeps its citation or "No evidence"; both themes; phone width works; no new runtime dependency except fonts.

## Direction contract

THESIS: Every piece of agent work is a drawing sheet with a title block that reads Drawn by an agent, Checked by the verifier, Approved by you. Refuses the category default of rounded cards on a SaaS gray canvas with an indigo accent.

OWN-WORLD: Whiteprint by day (cool drafting-film paper, India-ink lines, diazo-blue references), blueprint by night (Prussian-blue sheet, chalk-white lines). Hairline-ruled tables in the bill-of-materials manner, condensed upper-case title-block lettering over a DIN-lineage sans, numbered balloon callouts for citations, redline only for what needs the person, crisp 2px corners, delta revision markers.

STORY: The owner sees at once what needs their signature, reads why (balloons open the exact source), signs or refuses, and can trace every step the crew took.

FIRST VIEWPORT: Left, the drawing register (sidebar: Today, Job search, Hiring, Control). Right, Today as a sheet: title strip with the date, a redline delta counting decisions, decisions as ruled rows with their primary action at the row's right edge, then "Done since last time" as a revision table.

FORM: Engineering drawing sheet, candidate 7 of 7 on the grounded list; seed key 63191463. Signature interaction: signing the title block's APPROVED cell lands an ink approval stamp. Motion grammar: 150 to 200 ms state changes, one stamp moment, rows update in place, nothing loops.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Raises (from declined challengers)

- Dance score: timeline and recorder bars take their length from real elapsed time.
- Botanical folio: original and tailored resume, posting and evidence sit side by side at one scale on a shared baseline.
- Character catalog: each crew role has one fixed identity plate (two-letter mark, role, one-line duty) reused everywhere.
- Split-flap: live data updates cells in place; columns and rows never jump.

## Unresolved

- Owner may still pick another direction on the decision page (choice made unattended after an hour without an answer).
