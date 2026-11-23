# Recorded answers for the evidence-scoring tests

Everything in this folder is invented test data. It is not model output, and the people, employers and postings the
answers refer to do not exist.

- The candidate and the posting these answers refer to are written out in `../../test_evidence.py` (the facts
  `fact-role`, `fact-python` and `fact-sql`, and the requirements `r1` to `r3`).
- The files use the format of the gateway's fake provider (`backend/app/platform/llm/fake.py`): a JSON list of answers,
  handed out in order, one per provider request. A string is the model's text verbatim, which is how a test scripts
  output that is not valid.

| File | What it scripts |
|---|---|
| `valid.json` | one valid answer: `r1` partly met by the role, `r2` met by Python, `r3` with no evidence |
| `invalid_twice.json` | two answers that do not fit the schema: text that is not JSON, then a verdict that is not one of the four |
