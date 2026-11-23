"""The benchmark's metrics and its paired bootstrap, as pure functions (README "Metrics (pre-registered)").

A list is the method's jobs in rank order, as refs. `labels` maps a ref to the grade (0 to 3) a labeller gave that job
for this profile; a job that is not there is unjudged and counts as grade 0. Nothing here reads a database or a file.
"""

import math
import random
from collections.abc import Mapping, Sequence

K = 5  # the headline cut-off: ndcg_at_5 and precision_at_5
RELEVANT = 2  # a grade of 2 or 3 is relevant (GUIDELINE.md)
RECALL_CUTS = (300, 40)
RESAMPLES = 2000
SEED = 20261123  # the date the metrics were pre-registered
CONFIDENCE = 0.95


def dcg(grades: Sequence[int]) -> float:
    """Discounted cumulative gain: gain 2^grade - 1, discount 1 / log2(position + 1), positions counted from 1."""
    return sum((2**grade - 1) / math.log2(position + 2) for position, grade in enumerate(grades))


def ndcg_at_k(grades: Sequence[int], ideal: Sequence[int], k: int = K) -> float | None:
    """DCG of the first k grades over the DCG of the best first k the profile's labels allow; None when that is 0
    (nothing is labelled 1 or more), so the profile says nothing about ordering."""
    best = dcg(sorted(ideal, reverse=True)[:k])
    return dcg(list(grades)[:k]) / best if best else None


def precision_at_k(grades: Sequence[int], k: int = K) -> float:
    """Relevant jobs in the first k, over k: a list shorter than k is still divided by k."""
    return sum(grade >= RELEVANT for grade in list(grades)[:k]) / k


def recall_at_k(grades: Sequence[int], relevant_total: int, k: int) -> float | None:
    """Relevant jobs in the first k over every relevant labelled job of the profile; None when there is none."""
    return sum(grade >= RELEVANT for grade in list(grades)[:k]) / relevant_total if relevant_total else None


def score_list(ranked: Sequence[str], labels: Mapping[str, int]) -> dict[str, float | int | None]:
    """Every per-profile metric of one method's list."""
    grades = [labels.get(ref, 0) for ref in ranked]
    relevant = sum(grade >= RELEVANT for grade in labels.values())
    return {
        "ndcg_at_5": ndcg_at_k(grades, list(labels.values())),
        "precision_at_5": precision_at_k(grades),
        **{f"recall_at_{k}": recall_at_k(grades, relevant, k) for k in RECALL_CUTS},
        "unjudged_at_5": sum(ref not in labels for ref in ranked[:K]),
        "listed": len(ranked),
    }


def mean(values: Sequence[float | None]) -> float | None:
    """The mean of the values that exist (a profile with no signal is None and left out); None when none exist."""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest rank: the value at rank ceil(p / 100 * n) of the sorted values (latency p50 and p95)."""
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]


def bootstrap(values: Sequence[float], *, resamples: int = RESAMPLES, seed: int = SEED) -> tuple[float, float]:
    """The 95% interval of the mean over profiles, by resampling the profiles with replacement. The percentiles are
    nearest rank. The same seed gives the same interval, so a re-run of the benchmark repeats it."""
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    tail = (1 - CONFIDENCE) / 2
    return means[math.ceil(tail * resamples) - 1], means[math.ceil((1 - tail) * resamples) - 1]


def paired_difference(
    method: Sequence[float | None], baseline: Sequence[float | None]
) -> tuple[float, tuple[float, float]] | None:
    """Mean difference (method minus baseline) over the profiles where both have a value, with its bootstrap interval;
    None when no profile has both."""
    pairs = [m - b for m, b in zip(method, baseline, strict=True) if m is not None and b is not None]
    if not pairs:
        return None
    return sum(pairs) / len(pairs), bootstrap(pairs)
