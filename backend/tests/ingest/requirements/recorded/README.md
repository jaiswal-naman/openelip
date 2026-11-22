# Recorded answers for the requirement tests

Everything in this folder is invented test data. It is not model output and it is not a real posting.

- The posting these answers refer to is written out in `../test_task_fake.py`; its company, Acme Robotics, does not
  exist. Segment numbers in the answers (3, 4, 99, ...) count the lines of that invented text.
- The files use the format of the gateway's fake provider (`backend/app/platform/llm/fake.py`): a JSON list of answers,
  handed out in order, one per provider request. A file that runs out raises, which is how a test proves that no
  further call was made.
- Each answer is the JSON object the task's schema describes (`app/ingest/requirements/schema.py`).

| File | What it scripts |
|---|---|
| `invented_valid.json` | one answer with two valid items, one with a quote and one without |
| `invented_every_item_cites_a_missing_segment.json` | two answers whose items all cite a segment that does not exist |
| `invented_missing_then_valid.json` | an unusable answer, then a valid one |
| `invented_partly_valid.json` | one answer with a valid item and one that cites a missing segment |
