# Chat service test fixtures

Everything here is invented: the candidates (Ada and Bob), the employers ("Initech Demo", "Globex Demo", "Hooli Demo",
"Umbrella Demo") and every address (`example.test`). No model is called: each recording is the fake provider's format, a
JSON list of the model's answers consumed in order, every task from the one file (`app.platform.llm.fake`).

| File | What it is |
|---|---|
| `world.py` | The shared world of the tool, service, route and stream tests: two candidates with committed rows, the real gateway on the fake provider (the cost ledger is the real one), a way to hold a reply open, and the parser of an event-stream body |
| `plain_reply.json` | One `chat.turn` answer: a reply, no tool |
| `ask_filtered.json` | `list_matches` for filtered decisions, then a reply citing the decision `[[d1]]` and its constraint `[[c1]]` |
| `propose_pune.json` | `propose_record_change`, the `record.propose` answer it asks for (a Pune preference quoting "I would also take jobs in Pune"), then a reply |
| `yes_send_it.json` | `get_applications`, then a reply that sends and approves nothing |
| `cites_unknown.json` | A reply that cites two aliases no tool returned (`[[d9]]`, `[[j4]]`) |
| `two_messages.json` | Two messages' worth of answers: `list_matches` and a reply citing `[[d1]]`, then `get_decision` on `d1` (an alias from the first message) and a reply citing it again |
| `open_job.json` | Two replies that cite the job alias `j1` with no tool: the first message gets `j1` from `context_job_id`, the second has none and relies on the session remembering it |

A `chat.turn` answer is a `ChatStep` (`app.chat.schemas`): `{"tool_calls": [...], "reply": null}` or
`{"tool_calls": [], "reply": "..."}`. A `record.propose` answer is a `ProposalList`: `{"proposals": [...]}`.
The aliases a recording cites (`d1`, `c1`, `j1`) are the ones the world's rows get: numbering is per kind, in order of
first appearance.
