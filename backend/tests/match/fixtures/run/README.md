# Recorded answers and helpers for the match run tests

Everything in this folder is invented test data. It is not model output, and the people, employers and postings the
answers refer to do not exist.

- The run tests build their own candidate and postings (`world.py`). The candidate's confirmed facts are, in the order
  the request numbers them: `f1` a role, `f2` "Built nightly Python and SQL pipelines ...", `f3` "Led a team of three
  engineers." and `f4` the skill "Python". Every posting has two required requirements, `r1` "Python" and `r2` "SQL".
- The recordings use the format of the gateway's fake provider (`backend/app/platform/llm/fake.py`): a JSON list of
  answers, handed out in order, one per provider request. A string is the model's text verbatim, which is how a test
  scripts output that is not valid.

| File | What it scripts |
|---|---|
| `all_met.json` | eight identical valid answers: both requirements met, citing `f2` |
| `unknown_alias_then_valid.json` | a first answer citing a fact alias that was never sent (rule 2), then a valid one |
| `bad_twice.json` | two answers that cite a fact alias that was never sent |
| `invalid_schema_twice.json` | two answers that are not JSON at all |
