"""The requirement_extraction suite as the eval runner calls it (backend/app/evals/runner.py: an OwnScorer entry).

score.py is the suite's scorer and its CI command (`score.py --record`). This file is the one thing the runner needs on
top of it, so that the suite has a live mode like the others:

    cd backend && uv run python -m app.evals run requirement_extraction --live
    cd backend && uv run python -m app.evals gate requirement_extraction --run <the id printed last>

A live run calls the real model the environment configures, through the gateway and the task that reads postings
(`job.requirements.extract`), over the 39 test postings (the test split without req-049) and records method "model",
split "test", the model's name and the prompt version beside the score. It applies the bars of README.md,
"Pre-registered bars": `req_spans_resolved` 1.0, `req_item_f1` at least 0.85 and `req_importance_accuracy` at least
0.90. `score` is 1.0 only when all three hold. The run is a measurement of the model: it opens and closes no feature
(AI_SYSTEM 2.2).

Without --live the runner replays recordings.jsonl as `score.py --method model --split test` does.
"""

import importlib.util
import sys
from pathlib import Path

from app.evals.runner import Result, Scored

HERE = Path(__file__).parent
SPLIT = "test"


def load_scorer():
    """This suite's score.py, by a name of its own (several suites have one)."""
    spec = importlib.util.spec_from_file_location("requirement_extraction_scorer_for_runner", HERE / "score.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scorer = load_scorer()
TASK = scorer.TASK
RECORDED = scorer.RECORDINGS
LIVE = True  # run(None) calls the real model: the runner's --live needs this to allow it
CONFIG = {
    "split": SPLIT,
    "labels": scorer.LABELS,
    "min_iou": scorer.MIN_IOU,
    "excluded": scorer.EXCLUDED,
    "prompt_version": scorer.PROMPT_VERSION,
}


def run(recorded: Path | None) -> Scored:
    """Score the test postings. `recorded` is a recording to replay through the gateway's fake provider; None is a live
    run, where the gateway calls the model the environment configures and nothing is replayed."""
    if recorded is None:
        done = scorer.score(scorer.load_gold(SPLIT), scorer.live_extractor)
        metrics = scorer.live_metrics(done["metrics"])
    else:
        done = scorer.run("model", SPLIT, scorer.stored_baseline("model", SPLIT), recorded)
        metrics = done["metrics"]
    results = [Result(item["key"], None, item["expected"], item["actual"], item["is_passed"]) for item in done["items"]]
    return Scored(results, metrics)
