# Campaign demo data (invented)

Everything here is invented. "Initech Demo" is a made-up company, its pages are made up, and no file describes a real
company, person or job. The demo is independent and not affiliated with any employer or service. `python -m app.cli demo
seed-campaign --email <address>` reads these files only on a scratch database (its name ends in `_test`) with
`LLM_PROVIDER=fake` and `RESEARCH_PAGES_DIR` set to `pages/`.

| Path | What it holds |
|---|---|
| `pages/apply.initech-demo.invalid/` | The "company site" the Researcher reads, served from disk instead of the network. The host is the first demo posting's apply link host (`https://apply.initech-demo.invalid/1`), on the reserved `.invalid` domain that can never resolve. `about.html` and `careers.html` hold the sentences the recorded Researcher answers quote. |
| `manifest.json` | The default recordings this demo needs in `app/platform/llm/recorded/` and the prompt version each was written for. A prompt edit makes `tests/test_demo_campaign.py` fail until the recording is checked. |

The recordings are wiring checks of the screens, not a measurement of any model. The Writer and the entailment answers
are the Phase 5 demo's, repeated: both demos run on the same account and first match, so either may run first in one
process and each finds an answer that fits it.
