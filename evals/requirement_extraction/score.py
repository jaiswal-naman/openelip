"""The requirement_extraction suite (IDX-08): of the requirements a person reads out of a posting, how many does the
extractor find, with the right words and importance, and does every stored citation resolve?

    uv run --project backend python evals/requirement_extraction/score.py --method rules --split dev

    DATABASE_URL_WORKER=.../name_test uv run --project backend python evals/requirement_extraction/score.py --split dev

A live run, with a real model answering, is not run from here: it is `python -m app.evals run requirement_extraction
--live` (suite.py), which calls the same task path and applies the live bars below (README, "Pre-registered bars").

--method model (the default) replays recordings.jsonl through the real task path: the gateway's fake provider hands
each recorded answer to the validator, which computes every span. The recordings are built from the heading rules by
record_fixture.py, so a replay proves that the code path works and guards it in CI; it is not a model quality number.
--method rules scores the heading rules themselves on the posting text: a real measurement, of the baseline. The bars
are in README.md, written before the first run. The labels are drafted by an agent and the maintainer's review is
pending, so every report says so.

    DATABASE_URL_WORKER=.../name_test uv run --project backend python evals/requirement_extraction/score.py --record

--record writes the run through the eval harness's tables (eval_runs and one eval_items row per posting, as the worker
role), prints its id as the last line and exits 0; `python -m app.evals gate requirement_extraction --run ID` judges
it, as CI does. --report PATH also stores the recorded run in that baseline file (see baseline.json).

--record, and a replay (which writes the gateway's cost ledger), write to the database DATABASE_URL_WORKER names, and a
recorded run cannot be deleted: both refuse unless that is a scratch database (its name ends in _test, or is `evals`,
the CI eval job's). --live writes to any other.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.evals import scratch
from app.evals.runner import git_sha
from app.ingest.api import rules_extract, segments
from app.ingest.requirements.task import PROMPT_VERSION, TASK, render
from app.ingest.requirements.task import run as run_task
from app.platform import config, db
from app.platform.llm import AIDisabled, SchemaInvalid, fake

SUITE = "requirement_extraction"
HERE = Path(__file__).parent
GOLD, RECORDINGS, BASELINE = (HERE / name for name in ("gold.jsonl", "recordings.jsonl", "baseline.json"))
FIXTURE = "rules_fixture"  # the only recording source this scorer judges (README, bars)
MIN_IOU = 0.5  # the overlap at which a predicted and a gold item are the same requirement
# The bars of a live-model run on the test split, written before any run (README, "Pre-registered bars", 2: the targets
# of AI_SYSTEM 4.3). They are for a real model; a replay and the rules method have none of them.
LIVE_ITEM_F1_BAR, LIVE_IMPORTANCE_BAR = 0.85, 0.90
# Test postings that are not unseen data. Left out of every test score, kept in gold.jsonl and in the recordings.
EXCLUDED = {"req-049": "a near-copy of dev posting req-001 (99.9% the same text)"}
LABELS = "agent-drafted, maintainer review pending"  # recorded in the run's config
REVIEW_NOTE = "labels drafted by an agent, review pending"  # printed beside every number
BASELINE_NOTE = "rules baseline, not a model quality number"
REPLAY_NOTE = "recorded replay of answers built from the heading rules: it proves the code path, not model quality"

Extractor = Callable[[dict[str, Any]], tuple[Sequence[Any], dict[str, int]]]  # posting -> rows, problems found


class RecordingError(Exception):
    """The recordings cannot be replayed for this run: the message names the posting and what is wrong."""


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """One JSON object per line. Iterating the file splits on newlines only, which a JSON string cannot contain."""
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def load_gold(split: str, path: Path = GOLD) -> list[dict[str, Any]]:
    """The postings of one split, text and labels as gold.jsonl holds them, without the EXCLUDED ones."""
    return [row for row in read_jsonl(path) if row["split"] == split and row["posting_id"] not in EXCLUDED]


# ---- scoring ----------------------------------------


def iou(a: tuple[Any, ...], b: tuple[Any, ...]) -> float:
    """Intersection over union of two half-open character spans, given as (start, end, ...)."""
    inside = min(a[1], b[1]) - max(a[0], b[0])
    return 0.0 if inside <= 0 else inside / ((a[1] - a[0]) + (b[1] - b[0]) - inside)


def match(predicted: list[tuple[int, int, str]], gold: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    """One-to-one pairs (predicted index, gold index): the greatest overlap first, only at MIN_IOU or more. Ties go to
    the earlier items, so the same input always gives the same pairs."""
    overlaps = sorted(
        (-overlap, i, j)
        for i, p in enumerate(predicted)
        for j, g in enumerate(gold)
        if (overlap := iou(p, g)) >= MIN_IOU
    )
    pairs: list[tuple[int, int]] = []
    taken_predicted: set[int] = set()
    taken_gold: set[int] = set()
    for _, i, j in overlaps:
        if i not in taken_predicted and j not in taken_gold:
            pairs.append((i, j))
            taken_predicted.add(i)
            taken_gold.add(j)
    return pairs


def resolves(description: str, row: Any) -> bool:
    """A row is a citation only if its text is the posting's text at its span (invariant 2)."""
    return 0 <= row.start < row.end <= len(description) and description[row.start : row.end] == row.text


def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for correct out of n (AI_SYSTEM 12.1)."""
    if n == 0:
        return 0.0, 1.0
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def ratio(top: int, bottom: int) -> float:
    return top / bottom if bottom else 0.0


def interval(correct: int, n: int) -> list[float]:
    return [round(end, 4) for end in wilson(correct, n)]


def score(postings: list[dict[str, Any]], extract: Extractor, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the extractor over the postings. Returns the metrics and one item per posting. `baseline` is the stored
    replay's metrics, which a replay of the same split may not fall below."""
    items: list[dict[str, Any]] = []
    total: Counter[str] = Counter()
    problems_found: Counter[str] = Counter()
    for posting in postings:
        rows, problems = extract(posting)
        predicted = [(row.start, row.end, row.importance) for row in rows]
        gold = [(g["start"], g["end"], g["importance"]) for g in posting["requirements"]]
        pairs = match(predicted, gold)
        resolved = sum(resolves(posting["description"], row) for row in rows)
        total.update(
            predicted=len(predicted),
            gold=len(gold),
            matched=len(pairs),
            importance_right=sum(predicted[i][2] == gold[j][2] for i, j in pairs),
            resolved=resolved,
        )
        problems_found.update(problems)
        items.append(
            {
                "key": posting["posting_id"],
                "expected": {"items": [list(g) for g in gold]},
                "actual": {"items": [list(p) for p in predicted], "rejected": dict(problems)},
                # Clean: every predicted row resolves and the validator turned nothing away.
                "is_passed": resolved == len(rows) and not problems,
                "score": round(ratio(2 * len(pairs), len(predicted) + len(gold)), 4),  # the posting's own item F1
            }
        )
    return {"metrics": _metrics(items, total, problems_found, baseline), "items": items}


def _metrics(
    items: list[dict[str, Any]], total: Counter[str], problems: Counter[str], baseline: dict[str, Any] | None
) -> dict[str, Any]:
    predicted, gold, matched = total["predicted"], total["gold"], total["matched"]
    metrics: dict[str, Any] = {
        "n": len(items),
        "passed": sum(item["is_passed"] for item in items),
        "predicted_items": predicted,
        "gold_items": gold,
        "matched_items": matched,
        "req_item_f1": round(ratio(2 * matched, predicted + gold), 4),
        "precision": round(ratio(matched, predicted), 4),
        "recall": round(ratio(matched, gold), 4),
        "req_importance_accuracy": round(ratio(total["importance_right"], matched), 4),
        # Not rounded, so that 99,999 of 100,000 does not read as 1.0. With no row at all nothing failed to resolve.
        "req_spans_resolved": total["resolved"] / predicted if predicted else 1.0,
        "intervals": {
            "precision": interval(matched, predicted),
            "recall": interval(matched, gold),
            "req_importance_accuracy": interval(total["importance_right"], matched),
        },
        "rejected": dict(problems),
    }
    held = {"req_spans_resolved": total["resolved"] == predicted}
    if baseline is not None:
        for name in ("req_item_f1", "req_importance_accuracy"):
            held[f"{name}_not_below_baseline"] = metrics[name] >= baseline[name]
    bars_met = bool(items) and all(held.values())
    # The harness gate reads `score`: 1.0 only when every bar that applies holds.
    return {**metrics, "bars": held, "bars_met": bars_met, "score": 1.0 if bars_met else 0.0}


# ---- the two methods ----------------------------------------


def rules_extractor(posting: dict[str, Any]) -> tuple[Sequence[Any], dict[str, int]]:
    return rules_extract(posting["description"]), {}


def request_hash(description: str) -> str:
    """What a recorded answer answers: a hash of the exact messages the task sends for this posting."""
    messages = json.dumps(render(segments(description)), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(messages.encode("utf-8")).hexdigest()


def load_recordings(path: Path = RECORDINGS) -> dict[str, dict[str, Any]]:
    lines = read_jsonl(path)
    if other := sorted({line["source"] for line in lines} - {FIXTURE}):
        raise RecordingError(f"{path.name}: source {other} is not {FIXTURE!r}; only fixture replays are scored here")
    return {line["posting_id"]: line for line in lines}


def replay_extractor(recordings: dict[str, dict[str, Any]], folder: Path) -> Extractor:
    """The real task path with the fake provider: each posting is answered from its own recorded answers, so a retry
    of one posting can never use up another's. An answer invalid twice is that posting's failure; the run goes on."""

    def extract(posting: dict[str, Any]) -> tuple[Sequence[Any], dict[str, int]]:
        posting_id, description = posting["posting_id"], posting["description"]
        if (recorded := recordings.get(posting_id)) is None:
            raise RecordingError(f"{posting_id}: no recorded answer")
        if recorded["request_sha256"] != request_hash(description):
            raise RecordingError(f"{posting_id}: recorded for another request (the prompt or the segmenter changed)")
        answers = folder / f"{posting_id}.json"
        answers.write_text(json.dumps(recorded["answers"]), encoding="utf-8")
        fake.replay_from(answers)  # starts this posting's list over
        try:
            result = run_task(description)
        except SchemaInvalid:
            return [], {"extraction_failed": 1}
        except RuntimeError as ran_out:
            if "no recorded answer left" not in str(ran_out):  # the fake provider's own message
                raise
            raise RecordingError(f"{posting_id}: the task asked for more answers than were recorded") from ran_out
        return result.rows, result.rejected

    return extract


def live_extractor(posting: dict[str, Any]) -> tuple[Sequence[Any], dict[str, int]]:
    """The real task path with the model the environment configures: nothing is replayed. An answer invalid twice is
    that posting's failure and the run goes on; a provider that stops answering ends the run in error."""
    try:
        result = run_task(posting["description"])
    except SchemaInvalid:
        return [], {"extraction_failed": 1}
    return result.rows, result.rejected


def live_metrics(measured: dict[str, Any]) -> dict[str, Any]:
    """The metrics of a live run: the three bars of the README, and `score` 1.0 only when every one holds."""
    held = {
        "req_spans_resolved": measured["req_spans_resolved"] == 1.0,
        "req_item_f1": measured["req_item_f1"] >= LIVE_ITEM_F1_BAR,
        "req_importance_accuracy": measured["req_importance_accuracy"] >= LIVE_IMPORTANCE_BAR,
    }
    bars_met = measured["n"] > 0 and all(held.values())
    return {
        **measured,
        "bars": held,
        "bars_met": bars_met,
        "score": 1.0 if bars_met else 0.0,
        "harness_check_only": False,  # a model answered: this is a measurement
    }


def stored_baseline(method: str, split: str, path: Path = BASELINE) -> dict[str, Any] | None:
    """The metrics a replay of this split may not fall below: the stored replay, if it is of the same split."""
    if method != "model" or not path.exists():
        return None
    stored = json.loads(path.read_text(encoding="utf-8")).get("runs", {}).get("replay")
    return stored["metrics"] if stored and stored["config"]["split"] == split else None


def run(
    method: str, split: str, baseline: dict[str, Any] | None = None, recordings: Path = RECORDINGS
) -> dict[str, Any]:
    """Score one split by one method. The replay needs the fake provider and the worker's database (the gateway writes
    its ledger); the rules need neither."""
    postings = load_gold(split)
    if method == "rules":
        return score(postings, rules_extractor, baseline)
    with tempfile.TemporaryDirectory() as folder:
        try:
            result = score(postings, replay_extractor(load_recordings(recordings), Path(folder)), baseline)
        finally:
            fake.replay_from(None)
    result["metrics"]["harness_check_only"] = True  # a fixture replay says nothing about quality
    return result


# ---- recording the run ----------------------------------------


def run_config(method: str, split: str) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "mode": "replay" if method == "model" else "code",  # no live model is called either way
        "method": method,
        "split": split,
        "labels": LABELS,
        "min_iou": MIN_IOU,
        "excluded": EXCLUDED if split == "test" else {},
    }
    if method == "model":
        settings |= {
            "task": TASK,
            "prompt_version": PROMPT_VERSION,
            "recording": RECORDINGS.name,
            "recording_source": FIXTURE,
        }
    return settings


def record(result: dict[str, Any], settings: dict[str, Any]) -> str:
    """Write the run as the harness does (eval_runs, one eval_items row per posting, then the frozen verdict) and
    return its id. The database is the one DATABASE_URL_WORKER names."""
    metrics = result["metrics"]
    with db.transaction(role="worker") as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO eval_runs (suite, git_sha, config) "
                "VALUES (CAST(:suite AS eval_suite), :sha, CAST(:config AS jsonb)) RETURNING id"
            ),
            {"suite": SUITE, "sha": git_sha(), "config": json.dumps(settings)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO eval_items (eval_run_id, item_key, expected, actual, is_passed, score) "
                "VALUES (:run, :key, CAST(:expected AS jsonb), CAST(:actual AS jsonb), :passed, :score)"
            ),
            [
                {
                    "run": run_id,
                    "key": item["key"],
                    "expected": json.dumps(item["expected"]),
                    "actual": json.dumps(item["actual"]),
                    "passed": item["is_passed"],
                    "score": item["score"],
                }
                for item in result["items"]
            ],
        )
        conn.execute(
            text(
                "UPDATE eval_runs SET status = CAST(:status AS eval_status), metrics = CAST(:metrics AS jsonb), "
                "is_gate_passed = :gate, finished_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {
                "status": "passed" if metrics["bars_met"] else "failed",
                "metrics": json.dumps(metrics),
                "gate": metrics["bars_met"],
                "id": str(run_id),
            },
        )
    return str(run_id)


def recorded_report(run_id: str, method: str) -> dict[str, Any]:
    """The run as the database holds it: what baseline.json publishes, so no number is typed in."""
    with db.transaction(role="worker") as conn:
        stored = (
            conn.execute(
                text(
                    "SELECT id::text AS run_id, suite::text AS suite, git_sha, config, status::text AS status, "
                    "is_gate_passed, finished_at, metrics FROM eval_runs WHERE id = CAST(:id AS uuid)"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        unclean = [
            row.item_key
            for row in conn.execute(
                text(
                    "SELECT item_key FROM eval_items "
                    "WHERE eval_run_id = CAST(:id AS uuid) AND NOT is_passed ORDER BY id"
                ),
                {"id": run_id},
            )
        ]
    return {
        **{key: stored[key] for key in ("run_id", "suite", "status", "is_gate_passed", "git_sha")},
        "finished_at": stored["finished_at"].isoformat(),
        "labels": REVIEW_NOTE,
        "note": BASELINE_NOTE + (f"; {REPLAY_NOTE}" if method == "model" else ""),
        "config": stored["config"],
        "metrics": stored["metrics"],
        "unclean_items": unclean,  # postings with an unresolved row or an item the validator turned away
    }


def store_baseline(path: Path, report: dict[str, Any]) -> None:
    """Put the recorded run in the baseline file under its own name, leaving the other runs in it as they are."""
    key = "replay" if report["config"]["method"] == "model" else "rules"
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stored["note"] = f"Both runs are baselines: {BASELINE_NOTE}; {REVIEW_NOTE}."
    stored.setdefault("runs", {})[key] = report
    path.write_text(json.dumps(stored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


# ---- the command line ----------------------------------------


def describe(method: str, split: str, metrics: dict[str, Any]) -> str:
    what = f"{BASELINE_NOTE}: the heading rules on the posting text" if method == "rules" else REPLAY_NOTE
    skipped = ", ".join(f"{pid} ({why})" for pid, why in EXCLUDED.items()) if split == "test" else "none"
    ci = metrics["intervals"]
    held = ", ".join(f"{name} {'met' if ok else 'NOT MET'}" for name, ok in metrics["bars"].items())
    return (
        f"{SUITE} on the {split} split ({REVIEW_NOTE})\n"
        f"  {what}\n"
        f"  {metrics['n']} postings ({metrics['passed']} clean), left out: {skipped}\n"
        f"  items: {metrics['predicted_items']} predicted, {metrics['gold_items']} in the gold set, "
        f"{metrics['matched_items']} matched at IoU >= {MIN_IOU}\n"
        f"  precision {metrics['precision']:.4f} (95% Wilson {ci['precision'][0]:.4f} to {ci['precision'][1]:.4f}), "
        f"recall {metrics['recall']:.4f} (95% Wilson {ci['recall'][0]:.4f} to {ci['recall'][1]:.4f}), "
        f"req_item_f1 {metrics['req_item_f1']:.4f}\n"
        f"  req_importance_accuracy {metrics['req_importance_accuracy']:.4f} (95% Wilson "
        f"{ci['req_importance_accuracy'][0]:.4f} to {ci['req_importance_accuracy'][1]:.4f}) over the matched items\n"
        f"  req_spans_resolved {metrics['req_spans_resolved']:.4f}; "
        f"turned away by the validator: {metrics['rejected'] or 'nothing'}\n"
        f"  bars that apply: {held}: {'MET' if metrics['bars_met'] else 'NOT MET'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--method", choices=("model", "rules"), default="model")
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument("--record", action="store_true", help="write the run to the database DATABASE_URL_WORKER names")
    parser.add_argument("--report", type=Path, help="store the recorded run in this baseline file (needs --record)")
    parser.add_argument("--live", action="store_true", help="let a write go to a database that is not a scratch one")
    args = parser.parse_args(argv)
    if args.report and not args.record:
        parser.error("--report stores a recorded run: add --record")
    if (args.record or args.method == "model") and (why := scratch.refusal(args.live)):
        parser.error(why)
    if args.method == "model" and config.get().llm_provider != "fake":
        parser.error("a replay needs LLM_PROVIDER=fake; this command never calls a real model")
    try:
        result = run(args.method, args.split, stored_baseline(args.method, args.split))
    except RecordingError as error:
        print(f"error: {error}; run record_fixture.py to rebuild the recordings", file=sys.stderr)
        return 2
    except AIDisabled:
        print("error: the fake provider is refused when APP_ENV=production", file=sys.stderr)
        return 2
    print(describe(args.method, args.split, result["metrics"]))
    if not args.record:
        return 0 if result["metrics"]["bars_met"] else 1
    run_id = record(result, run_config(args.method, args.split))
    if args.report:
        store_baseline(args.report, recorded_report(run_id, args.method))
    print(run_id)  # the last line: CI hands it to the gate, which decides
    return 0


if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = "fake"  # a replay needs no model: the answers come from a recorded file
    config.get.cache_clear()
    sys.exit(main())
