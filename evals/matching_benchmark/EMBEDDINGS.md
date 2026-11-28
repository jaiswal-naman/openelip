# Embeddings: the local model against OpenAI's

Measured 2026-11-28. This is a first reading with real limits (below), not a result to quote without them.

**What was asked.** Does OpenAI's `text-embedding-3-large`, shortened to 1536 dimensions, order jobs for a profile better
than the local `BAAI/bge-small-en-v1.5` (384 dimensions) that the application used until now?

**How.** For each of the 20 invented profiles, the jobs that have a label for it (the 366 pooled pairs of `labels.jsonl`,
273 different postings) were ranked by the cosine similarity between the profile passage (the application's own
`profile_text`) and the job text (the application's own `embedding_text`: the title and the first 2,000 characters of the
description, because the snapshot has no extracted requirements). The ranking was scored with this folder's `ndcg_at_k`,
and each difference from bge-small was bootstrapped over the 20 profiles (10,000 resamples, seed 7, 95% percentile
interval, paired). The code is `embedding_compare.py`.

## Result

Mean ndcg over the 20 profiles. "vs bge" is the paired difference from bge-small at 384 dimensions, with its 95% interval.
An interval that includes 0 is not a detectable difference.

| Model, width, job text | ndcg@5 | vs bge | ndcg@10 | vs bge |
|---|---|---|---|---|
| bge-small, 384, first 2,000 characters (the baseline) | 0.684 | | 0.733 | |
| **text-embedding-3-large, 1536, first 2,000 characters** | **0.796** | **+0.112 (+0.058 to +0.166)** | **0.852** | **+0.119 (+0.079 to +0.162)** |
| text-embedding-3-large, 3072, first 2,000 characters | 0.790 | +0.106 (+0.054 to +0.160) | 0.850 | +0.117 (+0.077 to +0.160) |
| text-embedding-3-small, 1536, first 2,000 characters | 0.702 | +0.018 (-0.043 to +0.079) | 0.790 | +0.057 (+0.013 to +0.099) |
| text-embedding-3-small, 384, first 2,000 characters | 0.670 | -0.013 (-0.076 to +0.049) | 0.769 | +0.036 (-0.017 to +0.089) |

Width, for `text-embedding-3-large` with the first 2,000 characters (the best of the six at both cut-offs was 1536):

| Width | ndcg@5 | vs bge | ndcg@10 | vs bge |
|---|---|---|---|---|
| 384 | 0.746 | +0.062 (-0.014 to +0.137) | 0.814 | +0.081 (+0.024 to +0.141) |
| 768 | 0.758 | +0.074 (+0.025 to +0.125) | 0.835 | +0.101 (+0.060 to +0.147) |
| 1024 | 0.780 | +0.096 (+0.039 to +0.155) | 0.843 | +0.110 (+0.068 to +0.157) |
| **1536** | **0.796** | **+0.112 (+0.058 to +0.166)** | **0.852** | **+0.119 (+0.079 to +0.162)** |
| 2000 | 0.788 | +0.104 (+0.055 to +0.152) | 0.848 | +0.115 (+0.075 to +0.159) |
| 3072 | 0.790 | +0.106 (+0.054 to +0.160) | 0.850 | +0.117 (+0.077 to +0.160) |

Embedding the whole posting (up to 20,000 characters) instead of the first 2,000 did not help: `text-embedding-3-large` at
3072 scored 0.748 at 5 and 0.833 at 10 (against 0.790 and 0.850), and `text-embedding-3-small` at 1536 scored 0.703 and 0.786
(against 0.702 and 0.790). The full-posting rows are in the script's output, not in the tables above.

**Reading it.** `text-embedding-3-large` at 1536 is ahead of bge-small by about 0.11 ndcg at both cut-offs, and the
intervals exclude 0. `text-embedding-3-small` shows no detectable difference at 5 (+0.018) and a smaller one at 10
(+0.057, interval above 0). 1536 had the highest means of the six widths, but the widths from 1024 up differ by little
(0.780 to 0.796 at 5) next to the width of the intervals. No paired test between widths was run, so "1536 is better than
2000" is not claimed.

## Caveats

- **The labels are agent-drafted, maintainer review pending.** One agent wrote the profiles and judged every pooled pair
  (`README.md`); there is no second rater and no kappa yet. If the labels move, these numbers move.
- **Twenty invented profiles, 366 pairs.** The intervals are over 20 profiles, so they are wide, and a profile that
  changes the answer moves the interval a lot. The profiles and postings are in English.
- **Condensed-list ndcg.** Only each profile's 12 to 24 labelled jobs are ranked, not the 758 postings of the snapshot and
  not the whole index. It says how well a model orders jobs somebody already judged, not how well it finds the good jobs
  among thousands. It is not a recall measurement.
- **The pool came from bge-based methods.** The pairs that were labelled are the jobs that keyword, vector and hybrid
  retrieval on bge-small returned. A model that would find other good jobs gets no credit for them here, so any pool bias
  favours bge-small.
- **The job text is the text before requirements exist.** The application embeds the title and the extracted requirement
  texts once a posting has them. The snapshot has none, so this measures the title plus the start of the description.
- **The best width was picked after seeing six widths at two cut-offs.** The interval of 1536 does not account for that
  choice. 1536 is also a size the index can hold: pgvector indexes `vector` columns up to 2,000 dimensions.
- **The 1536 vectors were made by cutting the 3072-wide ones and scaling them back to length 1**, as OpenAI's embeddings
  guide shows. The application asks the API for 1536 with the `dimensions` parameter. The guide recommends that parameter
  but does not say the two give the same vectors, so this measurement strictly describes the cut vectors. It was not
  repeated with the parameter.
- **One run.** Nothing was repeated, and the latency of an OpenAI call was not measured.
- **The merged script has not been run against the API.** The tables above are the output of the two scratch scripts
  (the model comparison and the width comparison) that `embedding_compare.py` was merged and cleaned from. The merged
  script was run once against a stub in place of the OpenAI SDK: its local baseline reproduced 0.684 and 0.733, and the
  rest ran end to end. Run it with a key to reproduce the table.

## Cost

OpenAI's pricing page (read 2026-11-28, standard tier) lists `text-embedding-3-large` at USD 0.13 and
`text-embedding-3-small` at USD 0.02 per million input tokens. The two measurement runs used 840,570 and 107,411 input
tokens, about USD 0.063 and USD 0.014 at those prices (computed from the token counts OpenAI reported, not read from a bill).

For the application: embedding 10,000 postings at about 500 tokens each (an estimate, not measured on the index) is 5
million tokens, about USD 0.65 once, and after that only postings whose text changes. One search query is a few tokens.

## Where this goes

The decision and its consequences are in `docs/AI_SYSTEM.md` section 3.1: the application uses `text-embedding-3-large`
at 1536 when it runs on an OpenAI key and the local model otherwise (`EMBEDDING_PROVIDER` in `.env.example`). This
folder's own benchmark (`run.py`) stays on the local model, so its published numbers do not change.
