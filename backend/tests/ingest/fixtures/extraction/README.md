# Fixtures for the extraction tests

**Everything in this folder is invented test data.** Acme Robotics is not a real company, the postings are not real
postings, and the `.json` files are not model output: they are hand-written answers in the format the fake provider
replays (a JSON list consumed in order; a string is the model's text verbatim, anything else is a valid answer).
JSON has no comments and the fake provider's file is a bare list, so the notice lives here and in the file names
(`invented_*`).

The postings split into numbered lines the way `segments()` does:

| Posting | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| `invented_posting_v2.txt` | intro | What you'll need | 5+ years of Go | Bachelor's degree in CS | We offer free lunch |
| `invented_posting_v3.txt` | intro | What you'll need | 7+ years of Go | Experience with Kubernetes is a plus | We offer free lunch |

| Answer file | What it says |
|---|---|
| `invented_answer_v2.json` | two requirements of version 2: line 3 whole (5 years), line 4 by quote |
| `invented_answer_v3.json` | two requirements of version 3: line 3 by quote (7 years), line 4 by quote, preferred |
| `invented_answer_empty.json` | a valid answer that lists no requirement |
| `invented_answer_unusable.json` | two answers that are not JSON, so the gateway gives up after its one fresh call |
