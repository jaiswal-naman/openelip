"""The role_intake suite (plan 06-02; HIR-02, AI_SYSTEM 4.3 and 12.2): of the requirements a person reads out of an
employer's role description, how many does the extraction find, with the right words and importance, and does every
stored citation resolve?

The Phase 1 runner calls run() (this suite is an OwnScorer entry in backend/app/evals/runner.py), so a replay is one
eval_runs row with one eval_items row per description, gated like any other suite:

    cd backend && uv run python -m app.evals run role_intake
    cd backend && uv run python -m app.evals gate role_intake --run <the id printed last>
    cd backend && uv run python ../evals/role_intake/score.py [--recorded FILE]   # the same, refusing a live database

Every description goes through the real task path: the segmenter, the gateway (a ledger row per call), and the
validator, which computes each item's text and span from the description. Only the model is replaced: the answers come
from recorded.jsonl, which was built from the gold labels. So the score proves that the code path works and that a
damaged recording fails; it is NOT a model quality number (invariant 8). The scoring itself is the
requirement-extraction suite's (evals/requirement_extraction/score.py), loaded and used as it is; only the bars differ.
README.md holds the pre-registered bars.

The same path runs live, with the model the environment configures answering every description (run(None); LIVE below):

    cd backend && uv run python -m app.evals run role_intake --live

That run records method "model", the model's name and the prompt version, and applies the live bars of the README (the
targets of AI_SYSTEM 4.3, the same as for postings). It is a measurement of the model on invented, tidy employer text:
it opens and closes no feature (AI_SYSTEM 2.2).
"""

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from app.evals import runner, scratch
from app.evals.runner import Result, Scored
from app.platform import config
from app.platform.llm import fake

HERE = Path(__file__).parent
EVALS = HERE.parent
GOLD = HERE / "gold.jsonl"
RECORDED = HERE / "recorded.jsonl"  # the runner's default recording for this suite
SUITE = "role_intake"
FIXTURE = "labels_fixture"  # the only recording source this scorer judges: answers built from the labels, not a model's
LABELS = "agent-drafted, maintainer review pending"
# The bars of README.md "Pre-registered bars", written before any run. They are for a replay, which is a wiring check.
ITEM_F1_BAR, IMPORTANCE_BAR = 0.80, 0.80
LIVE = True  # run(None) calls the real model: the runner's --live (backend/app/evals/runner.py) needs this to allow it


def load_requirement_suite() -> ModuleType:
    """The requirement-extraction suite's score.py, by a name of its own (both suites call theirs score.py)."""
    name = "evals_requirement_extraction_score"
    spec = importlib.util.spec_from_file_location(name, EVALS / "requirement_extraction" / "score.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


phase2 = load_requirement_suite()
TASK = phase2.TASK  # the task whose route the run's record names: the one that reads postings
CONFIG = {
    "labels": LABELS,
    "gold": GOLD.name,
    "recording_source": FIXTURE,
    "prompt_version": phase2.PROMPT_VERSION,
    "bars": {"req_item_f1": ITEM_F1_BAR, "req_importance_accuracy": IMPORTANCE_BAR},
}


# What only a live run records: no recording, and the bars of a model (the README's "Pre-registered bars")
LIVE_CONFIG = {
    "recording_source": None,
    "bars": {"req_item_f1": phase2.LIVE_ITEM_F1_BAR, "req_importance_accuracy": phase2.LIVE_IMPORTANCE_BAR},
}


def load_recordings(path: Path) -> dict[str, dict[str, Any]]:
    lines = phase2.read_jsonl(path)
    if other := sorted({line["source"] for line in lines} - {FIXTURE}):
        raise phase2.RecordingError(
            f"{path.name}: source {other} is not {FIXTURE!r}; only fixture replays are scored here"
        )
    return {line["posting_id"]: line for line in lines}


def bars_held(item_f1: float, importance_accuracy: float, spans_resolved: float) -> dict[str, bool]:
    # Absolute: one stored citation that does not resolve fails the run, however good the rest is.
    return {
        "req_spans_resolved": spans_resolved == 1.0,
        "req_item_f1": item_f1 >= ITEM_F1_BAR,
        "req_importance_accuracy": importance_accuracy >= IMPORTANCE_BAR,
    }


def run(recorded: Path | None) -> Scored:
    """Score every description. `recorded` is a recording to replay through the gateway's fake provider; None is a live
    run, where the gateway calls the model the environment configures and nothing is replayed."""
    gold = phase2.read_jsonl(GOLD)
    if recorded is None:
        scored = phase2.score(gold, phase2.live_extractor)
        metrics = phase2.live_metrics(scored["metrics"])
    else:
        recordings = load_recordings(recorded)
        with tempfile.TemporaryDirectory() as folder:
            try:
                scored = phase2.score(gold, phase2.replay_extractor(recordings, Path(folder)))
            finally:
                fake.replay_from(None)
        measured = scored["metrics"]
        held = bars_held(measured["req_item_f1"], measured["req_importance_accuracy"], measured["req_spans_resolved"])
        bars_met = bool(scored["items"]) and all(held.values())
        metrics = {
            **measured,
            "bars": held,
            "bars_met": bars_met,
            "score": 1.0 if bars_met else 0.0,  # the gate reads this: 1.0 only when every bar holds
            "harness_check_only": True,  # a replay of answers built from the gold says nothing about quality
        }
    slice_of = {row["posting_id"]: row["slice"] for row in gold}
    results = [
        Result(i["key"], slice_of[i["key"]], i["expected"], i["actual"], i["is_passed"]) for i in scored["items"]
    ]
    return Scored(results, metrics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--recorded", type=Path, default=RECORDED, help="the recording to replay (default recorded.jsonl)"
    )
    args = parser.parse_args(argv)
    if why := scratch.refusal(live=False):  # a recorded run is frozen for good: only a scratch database may take it
        parser.error(why)
    try:
        done = runner.run_suite(SUITE, args.recorded)
    except runner.ReplayNeedsFake:
        parser.error("a replay needs LLM_PROVIDER=fake; this command never calls a real model")
    print(f"{SUITE}: {done.passed} of {done.n} descriptions passed, status {done.status}", file=sys.stderr)
    print(done.run_id)  # the last line: the gate takes it
    return 2 if done.status == "error" else 0


if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = "fake"  # a replay needs no model: the answers come from a recorded file
    config.get.cache_clear()
    sys.exit(main())
