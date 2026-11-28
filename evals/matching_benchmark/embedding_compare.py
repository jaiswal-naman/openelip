"""The local embedding model against OpenAI's, on the benchmark's labelled pairs (EMBEDDINGS.md has the result).

    cd backend
    OPENAI_API_KEY=... uv run python ../evals/matching_benchmark/embedding_compare.py

For each of the 20 invented profiles, the jobs labelled for it (the pooled pairs of labels.jsonl) are ranked by cosine
similarity between the profile passage and the job text, both made by the application's own functions, and scored with
the benchmark's ndcg. That is a condensed-list measure: only the labelled jobs are ranked, not the whole index. The
differences are paired by profile and bootstrapped over the 20 profiles.

It calls OpenAI (about 0.08 US dollars in all at the prices below, from the token counts OpenAI reports) with the key in
OPENAI_API_KEY, which is read by the SDK and never printed. It needs no database and writes nothing but --out.
Shortening a vector is done here, as OpenAI's embeddings guide shows: cut the 3072-wide vector and scale it back to
length 1. The application asks the API for 1536 with `dimensions` instead, and the guide does not say the two are
identical.
"""

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
os.environ["EMBEDDING_PROVIDER"] = "local"  # the baseline is the local model, whatever key the shell holds
sys.path[:0] = [str(REPO / "backend"), str(REPO)]

from evals.matching_benchmark.metrics import ndcg_at_k  # noqa: E402

from app.ingest.embedding import embedding_text  # noqa: E402
from app.match.retrieval import profile_text  # noqa: E402
from app.platform.llm import embed  # noqa: E402

FULL_CHARS = 20000  # about 5,000 tokens: the whole posting, under the 8,192-token input of OpenAI's embedding models
PRICE = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}  # USD per million input tokens, 2026-11-28
BASELINE = "bge-small, 384, first 2,000 chars"
CUTOFFS = (5, 10)
BATCH = 64
RESAMPLES, SEED = 10_000, 7


def rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (HERE / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def unit(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / length for x in vector]


def shorten(vectors: list[list[float]], width: int) -> list[list[float]]:
    return [unit(v[:width]) for v in vectors]


def interval(a: dict[str, float | None], b: dict[str, float | None]) -> tuple[float, float, float]:
    """Mean of a - b over the profiles that have a score in both, and its 95% paired percentile bootstrap interval."""
    keys = [k for k in a if a[k] is not None and b[k] is not None]
    rng = random.Random(SEED)
    means = sorted(
        sum(a[k] - b[k] for k in sample) / len(sample)  # type: ignore[operator]
        for sample in ([rng.choice(keys) for _ in keys] for _ in range(RESAMPLES))
    )
    mean = sum(a[k] - b[k] for k in keys) / len(keys)  # type: ignore[operator]
    return mean, means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "embedding_compare.json")
    out = parser.parse_args().out
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("set OPENAI_API_KEY (it is read by the OpenAI SDK and never printed)")
    import openai

    judged: dict[str, dict[str, int]] = {}
    for label in rows("labels.jsonl"):
        judged.setdefault(label["profile_key"], {})[label["job_ref"]] = label["grade"]
    jobs = {j["ref"]: j for j in rows("snapshot.jsonl")}
    profiles = {p["key"]: p for p in rows("profiles.jsonl") if p["key"] in judged}
    refs = sorted({ref for grades in judged.values() for ref in grades})
    keys = sorted(profiles)
    passages = [profile_text(profiles[k]["facts"], profiles[k]["preferences"]) for k in keys]
    texts = {
        "first 2,000 chars": [embedding_text(jobs[r]["title"], jobs[r]["description"], []) for r in refs],
        "full posting": [f"{jobs[r]['title']}\n{jobs[r]['description'][:FULL_CHARS]}" for r in refs],
    }

    spent = {"tokens": 0, "usd": 0.0}
    client = openai.OpenAI(timeout=120, max_retries=3)

    def from_openai(model: str, batch: list[str]) -> list[list[float]]:
        found: list[list[float]] = []
        for start in range(0, len(batch), BATCH):
            response = client.embeddings.create(model=model, input=batch[start : start + BATCH])
            found += [d.embedding for d in response.data]
            spent["tokens"] += response.usage.prompt_tokens
            spent["usd"] += response.usage.prompt_tokens * PRICE[model] / 1e6
        return found

    local = lambda batch: [unit(v) for v in embed.embed_passages(batch, model=embed.LOCAL_MODEL_ID)]  # noqa: E731
    # name -> (job vectors by ref, profile vectors by key)
    variants = {BASELINE: (dict(zip(refs, local(texts["first 2,000 chars"]))), dict(zip(keys, local(passages))))}
    for model, short_widths, full_widths in (
        ("text-embedding-3-small", (384, 1536), (384, 1536)),
        ("text-embedding-3-large", (384, 768, 1024, 1536, 2000, 3072), (384, 3072)),
    ):
        profile_full = from_openai(model, passages)
        for kind, widths in (("first 2,000 chars", short_widths), ("full posting", full_widths)):
            job_full = from_openai(model, texts[kind])
            for width in widths:
                variants[f"{model}, {width}, {kind}"] = (
                    dict(zip(refs, shorten(job_full, width))),
                    dict(zip(keys, shorten(profile_full, width))),
                )

    def score(job_vectors: dict, profile_vectors: dict, k: int) -> dict[str, float | None]:
        found = {}
        for key, grades in judged.items():
            p = profile_vectors[key]
            ranked = sorted(grades, key=lambda r: -sum(a * b for a, b in zip(p, job_vectors[r])))
            found[key] = ndcg_at_k([grades[r] for r in ranked], sorted(grades.values(), reverse=True), k)
        return found

    report: dict = {"pairs": sum(len(g) for g in judged.values()), "profiles": len(judged), "spent": spent, "rows": []}
    for k in CUTOFFS:
        base = score(*variants[BASELINE], k)
        for name, (job_vectors, profile_vectors) in variants.items():
            per = score(job_vectors, profile_vectors, k)
            present = [v for v in per.values() if v is not None]
            delta, low, high = interval(per, base)
            report["rows"].append(
                {"k": k, "model": name, "ndcg": round(sum(present) / len(present), 3),
                 "vs_bge": [round(delta, 3), round(low, 3), round(high, 3)]}
            )  # fmt: skip
            print(
                f"ndcg@{k}  {name:52s} {report['rows'][-1]['ndcg']:.3f}  vs bge {delta:+.3f} [{low:+.3f}, {high:+.3f}]"
            )
    print(f"OpenAI input tokens {spent['tokens']}, about USD {spent['usd']:.4f} at the prices in this file")
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
