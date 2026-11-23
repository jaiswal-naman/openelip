# Graph fixtures (plan 07-04)

Everything here is invented and labelled: the company "Lumen Freight", its pages (`pages/careers.lumen.example/`, served
by `RESEARCH_PAGES_DIR`), and the recorded model answers (`recorded/<task>.json`, served by `LLM_FAKE_DIR`).

The recordings are wiring checks, not model quality. `verify.entail` holds twelve identical `true` answers so that a
process restarted after a crash (the replay counter starts again) gives the same verdicts in every node.
