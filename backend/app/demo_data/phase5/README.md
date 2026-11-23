# Phase 5 demo data (invented)

Everything here is invented. "Demo Candidate", "Globex Demo" and "Initech Demo" are made-up names and no file describes a
real person, company or job. `python -m app.cli demo seed-tailor --email <address>` reads these files and writes them only
into a scratch database whose name ends in `_test`, with `LLM_PROVIDER=fake`, after `demo seed-matches` has run for the
same address.

| File | What it holds |
|---|---|
| `resume.txt` | A second invented resume. The seed stores it as a parsed document; each fact below quotes one of its lines. |
| `facts.json` | Two roles with two achievements each and three skills, as confirmed facts, plus two skills the candidate "said in chat" (confirmed proposals, so the base resume does not show them yet and a tailoring edit may add them). |
| `messages.json` | The two scripted chat messages the demo sends. The recordings answer exactly these. |
| `manifest.json` | For each default recording in `app/platform/llm/recorded/`, the task it answers and the prompt version it was written for. A prompt edit makes `tests/test_demo_seed_p5.py` fail until the recording is checked. |

## The recordings

The fake provider hands out each recording's answers in order, once per process. The API process serves `chat.turn` and
`record.propose` (message 1: a `list_matches` step, then a reply citing `[[d1]]`; message 2: a `propose_record_change`
step, one proposal, then a reply). The worker serves `tailor.propose` (five edits: three hold, two are discarded),
`writer.packet` and `verify.entail` (every packet sentence entailed). A demo stack therefore runs the whole demo once;
start a fresh one to run it again. The answers are a wiring check of the screens, not a measurement of any model.

Fact numbering as the requests see it: f1 to f3 the Phase 4 record, f4 to f9 this resume's two roles and their
achievements, f10 to f12 the three skills, f13 and f14 the skills said in chat.
