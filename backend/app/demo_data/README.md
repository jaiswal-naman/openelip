# Demo data (invented)

Everything in this folder is invented. The candidate, the employer ("Initech Demo"), the postings and the recorded model
answers describe no real person, company or job, and nothing here is model output. `python -m app.cli demo seed-matches`
reads these files and writes them only into a scratch database whose name ends in `_test`, with `LLM_PROVIDER=fake`.

| File | What it holds |
|---|---|
| `resume.txt` | The invented resume. The seed stores it as a parsed document; each confirmed fact quotes one of its lines. |
| `postings.json` | Four invented postings, each with four requirements whose texts occur word for word in its description. One is on site in Germany, which the demo candidate's location and remote-policy constraints exclude, so it is filtered, never shown. Every apply link is on the `.invalid` domain, which can never resolve. |
| `match.evidence.score.json` | The recorded answers the fake provider hands out, in order, one per posting that gets scored (three, because the fourth is filtered before any scoring). |

## About the recorded answers

The fake provider replays answers in the order the run scores postings, and which posting scores first depends on the
search ranking. So the three answers are identical and each is valid for any of the three postings: requirement `r1` of
every posting names a skill the candidate's fact `f2` ("Skills: Quillscript, PostgreSQL.") states, `r2` asks for 3+ years that
the role `f1` covers, `r3` asks for something the record never mentions, and `r4` asks for team management that the
achievement `f3` ("Led a team of three engineers.") only partly covers. Fact and requirement aliases are numbered as the
request numbers them: facts in resume order, requirements by ordinal.

The verdicts are a wiring check of the page and the brief, not a quality measurement of any model.
