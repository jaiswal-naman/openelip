# Chat test fixtures

Everything here is invented: no real person, resume or posting. Recordings are the fake provider's format, a JSON list of
the model's answers consumed in order (`app.platform.llm.fake`).

| File | What it is |
|---|---|
| `harness.py` | The real gateway on the fake provider, and recordings written from the answers a test shows. Imported by the chat tests (`from tests.chat.fixtures import harness`) |
| `loop_get_record.json` | One `chat.turn` script: a step that calls `get_record`, then a reply that cites fact `f1` |

A `chat.turn` answer is a `ChatStep` (`app.chat.schemas`): `{"tool_calls": [...], "reply": null}` or
`{"tool_calls": [], "reply": "..."}`. A `record.propose` answer is a `ProposalList`: `{"proposals": [...]}`.
