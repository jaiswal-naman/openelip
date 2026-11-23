# Hiring desk demo data (invented)

Everything in this folder is invented. "Halden Demo", the role, the seven candidates and the recorded model answers describe
no real person, company or job, and nothing here is model output. `python -m app.cli demo seed-hiring` reads these files
and writes them only into a scratch database whose name ends in `_test`, with `LLM_PROVIDER=fake`.

| File | What it holds |
|---|---|
| `role.txt` | The invented role the employer pastes into "Describe the role". Its requirement sentences occur word for word. |
| `record.json` | The invented resume lines and the confirmed facts every demo candidate gets, in a fixed order. |
| `recorded/job.requirements.extract.json` | What the fake provider answers when the role is read: three requirements, cited by line number. |
| `recorded/match.evidence.score.json` | What it answers when a candidate is scored against the role: identical answers. |

## About the recorded answers

`docker-compose.demo.yml` sets `LLM_FAKE_DIR` to the `recorded` folder here. The provider uses a file of this folder for
its task and `app/platform/llm/recorded/` for every other task. Each file is a list handed out in order, once per process.

**One demo per stack.** `LLM_FAKE_DIR` applies to the whole demo stack, so the matches and morning-brief demo would replay
these hiring answers on a stack where this demo has run, and the other way round. Run each demo on a fresh stack
(`down -v`, then `up`), as `.github/workflows/e2e.yml` does.

- **Requirements.** `role.txt` has six non-blank lines. Line 3 is "Python and SQL services in production." (required skill),
  line 4 "3+ years as a backend engineer." (required experience, 3 years) and line 5 "Experience leading a small team is a
  plus." (preferred). The answers are identical, so which of the (at most two) calls takes which does not matter.
- **Evidence.** Every candidate has the same facts, so the answers are identical and call order does not matter. Facts are
  numbered `f1`.. in resume order and requirements `r1`.. by ordinal: `r1` is met by `f2` (the skills line), `r2` by `f1`
  (the role, 2018 to 2024) and `r3` partly by `f3` (the team lead line). One run scores the seven candidates who pass the
  hard filter, one answer each; an answer that the validator refused would be asked for again, so the file holds 21.
