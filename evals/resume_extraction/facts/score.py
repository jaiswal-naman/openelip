"""The resume-facts suite: scores extract_facts against the gold set (plan 03-03; AI_SYSTEM 4.2 and 12.2).

The Phase 1 runner calls run() (backend/app/evals/runner.py: this suite is an OwnScorer entry in SUITES), so a replay is
recorded as one eval_runs row and gated like any other suite: `python -m app.evals run resume_extraction`, then
`gate resume_extraction --run ID`.

Every resume goes through the real gateway (a ledger row per call) and the real checks in code. Only the model is
replaced: the answers come from recordings.jsonl, which a script wrote from the gold truth plus injected faults. So the
score proves that the code path works and the faults are caught. It is not a model quality number (invariant 8).

Scoring. A kept fact is paired with the truth fact of its kind that it overlaps most (characters of the resume text),
each truth fact used once. Every field of a pair is a true positive when equal and a false positive plus a false
negative when not. An unpaired kept fact adds false positives, an unpaired truth fact false negatives; "kind" counts as
one field of every fact. F1 is 2 * tp / (2 * tp + fp + fn), from whole numbers, so a replay gives the same score on
every machine. The headline `score` is the overall F1; employer, title and dates (the fields that feed the tailoring
verifier) are reported on their own.

    python score.py    # development: replay the recordings and write baseline.json (needs a scratch test database)

The same path runs live, with the model the environment configures answering every resume (run(None); LIVE below):

    cd backend && uv run python -m app.evals run resume_extraction --live

That run records method "model", the model's name, the prompt versions and the split beside the score, and applies the
bars of AI_SYSTEM 4.2 as README.md states them: overall field F1 at least 0.90 and employer, title and dates F1 at least
0.95. Its `score` is 1.0 only when both hold (and 0.0 otherwise), so the gate's floor needs no change for it; the
replay's `score` stays the overall F1 against the baseline. A live run is a measurement of the model: it opens and
closes no feature (AI_SYSTEM 2.2).
"""

import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from sqlalchemy.engine import make_url

from app.evals.runner import Result, Scored
from app.platform import config, llm
from app.platform.llm import fake as provider
from app.profile.extract import fake, workflow
from app.profile.extract.fake import result_from_lines

HERE = Path(__file__).parent
GOLD = HERE / "gold.jsonl"
RECORDED = HERE / "recordings.jsonl"  # the runner's default recording for this suite
BASELINE = HERE / "baseline.json"
TASK = "resume.facts.roles"  # the task whose route the run's record names; the other two share its provider
CONFIG = {
    "slice": "facts",
    "synthetic": True,
    "labels": "invented resumes; the truth was written with them",
    "prompt_versions": workflow.prompt_versions(),
}
FIELDS = {
    "role": ("employer", "title", "start_date", "end_date"),
    "project": ("employer", "title", "start_date", "end_date"),
    "education": ("institution", "degree", "start_date", "end_date"),
    "certification": ("name", "end_date"),
    "skill": ("name",),
    "achievement": (),
    "other": (),
}
EMPLOYER_TITLE_DATES = frozenset({"employer", "title", "start_date", "end_date"})  # held to 0.95 (AI_SYSTEM 4.2)
LIVE = True  # run(None) calls the real model: the runner's --live (backend/app/evals/runner.py) needs this to allow it
# The bars of a live run, as README.md and AI_SYSTEM 4.2 state them (the targets of 12.1's pre-registration). A kept
# item whose quote does not match its span is no bar: QuoteMismatch ends the run in error instead.
F1_BAR, EMPLOYER_TITLE_DATES_BAR = 0.90, 0.95
# What only a live run records. The gold set has no development part: no model has ever answered on it, so none of
# it was tuned against and all 120 resumes are the test split (README.md, "What a live run measures").
LIVE_CONFIG = {"split": "test", "bars": {"f1_overall": F1_BAR, "f1_employer_title_dates": EMPLOYER_TITLE_DATES_BAR}}


class QuoteMismatch(Exception):
    """A kept fact whose source_quote is not the text at its source_span. The checks in code make this impossible, so
    it ends the run in error (and the gate refuses an errored run) instead of lowering a score."""


def load_gold():
    items = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("gold.jsonl has a repeated key")
    return items


def f1(tp, fp, fn):
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def same(a, b):
    return " ".join(str(a).split()).lower() == " ".join(str(b).split()).lower()


def truth_span(item, fact, starts):
    if "span" in fact:
        return tuple(fact["span"])
    first, last = fact["line_start"] - 1, fact["line_end"] - 1
    return starts[first], starts[last] + len(item["lines"][last])


def overlap(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def tally(item, kept):
    """Counts for one resume: {"all": [tp, fp, fn], "group": [tp, fp, fn]} (group is employer, title and dates)."""
    starts, total = [], 0
    for line in item["lines"]:
        starts.append(total)
        total += len(line) + 1
    truth = item["expected"]["facts"]
    spans = [truth_span(item, fact, starts) for fact in truth]
    paired = {}  # index in kept -> index in truth
    for k, fact in enumerate(kept):
        taken = set(paired.values())
        options = [
            (overlap(fact.source_span, spans[t]), -t)
            for t, f in enumerate(truth)
            if f["kind"] == fact.kind and t not in taken
        ]
        best = max(options, default=(0, 0))
        if best[0] > 0:
            paired[k] = -best[1]
    counts = {"all": [0, 0, 0], "group": [0, 0, 0]}

    def count(field, truth_value, kept_value):
        where = ["all"] + (["group"] if field in EMPLOYER_TITLE_DATES else [])
        if truth_value is not None and kept_value is not None and same(truth_value, kept_value):
            hits = (1, 0, 0)
        else:
            hits = (0, int(kept_value is not None), int(truth_value is not None))
        for name in where:
            counts[name] = [a + b for a, b in zip(counts[name], hits)]

    for k, fact in enumerate(kept):
        t = paired.get(k)
        wanted = truth[t] if t is not None else {}
        count("kind", fact.kind if t is not None else None, fact.kind)
        for field in FIELDS[fact.kind]:
            count(field, wanted.get(field), fact.attributes.get(field))
    for t, fact in enumerate(truth):
        if t not in paired.values():
            count("kind", fact["kind"], None)
            for field in FIELDS[fact["kind"]]:
                count(field, fact.get(field), None)
    return counts


def brief_truth(fact):
    fields = {name: fact[name] for name in FIELDS[fact["kind"]] if fact.get(name) is not None}
    return {"kind": fact["kind"], "lines": [fact["line_start"], fact["line_end"]], **fields}


def brief_kept(fact):
    fields = {name: fact.attributes[name] for name in FIELDS[fact.kind] if name in fact.attributes}
    return {"kind": fact.kind, "span": list(fact.source_span), **fields}


def score_items(gold, *, parallel):
    """Every resume through the real gateway and checks. The gateway answers however the environment configured it: from
    a recording in a replay (which needs parallel=False: the fake provider answers in call order), from the model when
    live (the three calls of a resume go together, as they do for a person's upload)."""
    results, totals, by_slice, seen = [], {"all": [0, 0, 0], "group": [0, 0, 0]}, {}, Counter()
    for item in gold:
        resume = result_from_lines(item["lines"])
        out = workflow.extract_facts(llm.complete, resume, attribution=None, parallel=parallel)
        for fact in out.kept:
            if resume.text[fact.source_span[0] : fact.source_span[1]] != fact.source_quote:
                raise QuoteMismatch(item["key"])
        counts = tally(item, out.kept)
        for name, found in counts.items():
            totals[name] = [a + b for a, b in zip(totals[name], found)]
        slice_counts = by_slice.setdefault(item["slice"], [0, 0, 0])
        by_slice[item["slice"]] = [a + b for a, b in zip(slice_counts, counts["all"])]
        seen.update(
            kept=len(out.kept),
            unverified=len(out.unverified),
            protected_skipped=out.protected_skipped,
            failed_calls=len(out.failed_calls),
        )
        _, fp, fn = counts["all"]
        results.append(
            Result(
                item["key"],
                item["slice"],
                {"facts": [brief_truth(f) for f in item["expected"]["facts"]]},
                {
                    "facts": [brief_kept(f) for f in out.kept],
                    "unverified": [u.reason for u in out.unverified],
                    "protected_skipped": out.protected_skipped,
                    "failed_calls": out.failed_calls,
                },
                fp == 0 and fn == 0,
            )
        )
    return results, totals, by_slice, seen


def run(recorded):
    """Score every resume. `recorded` is a recording to replay through the gateway's fake provider; None is a live run,
    where the gateway calls the model the environment configures and nothing is replayed."""
    gold = load_gold()
    if recorded is None:
        results, totals, by_slice, seen = score_items(gold, parallel=True)
    else:
        requests = [
            (task.name, messages)
            for item in gold
            for task, messages in workflow.build_requests(result_from_lines(item["lines"]))
        ]
        with tempfile.TemporaryDirectory() as directory:
            answers = fake.replay_file(requests, fake.load(recorded), Path(directory) / "answers.json")
            provider.replay_from(answers)  # the gateway's fake provider reads these, in the order the calls are made
            try:
                results, totals, by_slice, seen = score_items(gold, parallel=False)
            finally:
                provider.replay_from(None)
    metrics = {
        "score": f1(*totals["all"]),
        "f1_overall": f1(*totals["all"]),
        "f1_employer_title_dates": f1(*totals["group"]),
        "f1_by_slice": {name: f1(*found) for name, found in sorted(by_slice.items())},
        "quote_exact_share": 1.0,  # QuoteMismatch above is the alternative
        **seen,
        "tp": totals["all"][0],
        "fp": totals["all"][1],
        "fn": totals["all"][2],
        "replay_only": recorded is not None,
        "synthetic": True,
    }
    if recorded is None:
        held = {
            "f1_overall": metrics["f1_overall"] >= F1_BAR,
            "f1_employer_title_dates": metrics["f1_employer_title_dates"] >= EMPLOYER_TITLE_DATES_BAR,
        }
        # The gate reads `score`: 1.0 only when both pre-registered bars hold, so its replay floor never judges a model.
        metrics |= {"bars": held, "bars_met": all(held.values()), "score": 1.0 if all(held.values()) else 0.0}
    return Scored(results, metrics)


def main():
    """Replay the committed recordings and write baseline.json. Every call leaves a ledger row, so this needs a migrated
    scratch database: DATABASE_URL_WORKER must name one whose database ends in _test, never the real one."""
    url = os.environ.get("DATABASE_URL_WORKER")
    if not url or not (make_url(url).database or "").endswith("_test"):
        raise SystemExit("set DATABASE_URL_WORKER to a migrated scratch database whose name ends in _test")
    os.environ["LLM_PROVIDER"] = "fake"
    config.get.cache_clear()
    scored = run(RECORDED)
    note = (
        "Replay of recordings.jsonl through the real gateway and checks, over invented resumes. It proves the code "
        "path and that injected faults are caught. It is not a model quality number."
    )
    body = {"note": note, "n": len(scored.results), **scored.metrics}
    BASELINE.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {BASELINE.name}: score {scored.metrics['score']}")


if __name__ == "__main__":
    main()
