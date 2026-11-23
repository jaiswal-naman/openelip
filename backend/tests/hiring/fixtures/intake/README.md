# Fixtures for the role-intake tests

**Everything in this folder is invented test data.** Halden Demo is not a real company, the role text is not a real
posting, and the `.json` files are not model output: they are hand-written answers in the format the gateway's fake
provider replays (a JSON list consumed in order; a string is the model's text verbatim, anything else is a valid
answer). JSON has no comments and the fake provider's file is a bare list, so the notice lives here and in the file
names (`invented_*`).

The role text of `test_intake.py` splits into numbered lines the way `ingest.api.segments` does:

| Line | Text |
|---|---|
| 1 | an introduction with accented letters, a symbol and an emoji outside the Basic Multilingual Plane |
| 2 | What you'll need |
| 3 | 3+ years of Python. |
| 4 | Strong SQL skills. |
| 5 | Experience with Airflow is a plus. |
| 6 | We offer free lunch. |

| Answer file | What it says |
|---|---|
| `invented_answer_three.json` | three requirements: line 3 whole (3 years), line 4 by quote, line 5 by quote, preferred |
| `invented_answer_unusable.json` | two answers that are not JSON, so the gateway gives up after its one fresh call |
| `invented_answer_missing_segment_twice.json` | two valid answers whose every item cites a line that does not exist, so the task gives up after its one fresh call |
