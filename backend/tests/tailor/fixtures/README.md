# Recordings for the tailoring tests

Everything in this folder is invented test data. It is not model output, and the people, employers and tools the
answers refer to do not exist.

- `invented.py` writes the invented candidate the tests share: two roles with bullets, three skills, a degree, a
  certification and one skill stated in chat. Its aliases (`f1` to `f14`) are what the recordings use.
- `gateway.py` runs the model gateway on the fake provider with the ledger write replaced by a list.
- The `.json` files use the format of the fake provider (`backend/app/platform/llm/fake.py`): a JSON list of answers,
  handed out in order, one per provider request, for every task. A string is the model's text verbatim, which is how a
  test scripts output that is not valid.

| File | What it scripts |
|---|---|
| `mixed.json` | A `tailor.propose` answer with seven edits (a reorder, a remove, an add from a chat-stated fact, and four rephrases: one clean, one with a changed number, one that inflates scope, one that adds a tool), then the `verify.entail` answer for the two rephrases that pass layer 1 |
| `seven_reasons.json` | A `tailor.propose` answer with one edit for each of the seven discard reasons and one clean reorder, then the `verify.entail` answer for the one rephrase that reaches layer 2 |
