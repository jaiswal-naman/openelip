"""The tailoring_integrity suite (plan 05-02; AI_SYSTEM 4.7 and 12.2): replay each pair's recording through the real
propose_and_verify, which is the request builder, the gateway's fake provider, layer 1 and, for the rephrases that
survive it, one verify.entail call, and score every returned edit against its label.

The Phase 1 runner calls run() (this suite is an OwnScorer entry in backend/app/evals/runner.py), so a replay is one
eval_runs row with one eval_items row per pair, gated like any other suite:

    cd backend && uv run python -m app.evals run tailoring_integrity
    cd backend && uv run python -m app.evals gate tailoring_integrity --run <the id printed last>
    cd backend && uv run python ../evals/tailoring_integrity/score.py [--recorded PATH]   # the same, recorded

The default recording is the folder recordings/: one file per pair, because a pair's calls are made in order (propose,
then entail when the labels say so) and a fault in one pair must not shift the answers of the next. A single file as
--recorded is the entailment fault: a list with one entry per pair (null where no entailment call is expected) that
replaces the real entailment answers (recordings/degraded_entail.json). Only the model is replaced. The score proves
that the code path works and that a fault fails; it is NOT a model quality number (invariant 8). README.md holds the
pre-registered bars.
"""

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple

from app.evals import runner, scratch
from app.evals.runner import Result, Scored
from app.platform import config
from app.platform.llm import SchemaInvalid
from app.platform.llm import fake as provider
from app.tailor import pipeline
from app.tailor.pipeline import VerifiedEdit
from app.tailor.resume import Fact
from app.tailor.verifier import Requirement

HERE = Path(__file__).parent
GOLD = HERE / "gold.jsonl"
RECORDINGS = HERE / "recordings"
RECORDED = RECORDINGS  # the runner's default recording for this suite: a folder, one file per pair
BASELINE = HERE / "baseline.json"
SUITE = "tailoring_integrity"
TASK = pipeline.PROPOSE_TASK  # the task whose route the run's record names
LABELS = "agent-drafted, maintainer review pending"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_gold() -> list[dict[str, Any]]:
    items = read_jsonl(GOLD)
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("gold.jsonl has a repeated key")
    return items


CONFIG = {
    "labels": LABELS,
    "prompt_versions": {pipeline.PROPOSE_TASK: pipeline.PROPOSE_VERSION, pipeline.ENTAIL_TASK: pipeline.ENTAIL_VERSION},
    "gold": GOLD.name,
    "allow_rephrase": {item["key"]: item["allow_rephrase"] for item in load_gold()},
}


class Outcome(NamedTuple):
    """What one pair came to: the edits the pipeline returned, or why it returned none."""

    edits: list[VerifiedEdit] | None
    failure: str | None  # None when edits came back; "schema_invalid" when the model's answer was invalid twice


@dataclass
class Tally:
    """The counts of one run. `kept` is supported edits that came back passed; `passed` is unsupported edits that came
    back passed, which is the first bar."""

    edits: int = 0
    supported: int = 0
    unsupported: int = 0
    kept: int = 0
    passed: int = 0
    discarded: int = 0
    reason_matched: int = 0
    failed_pairs: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)
    passed_edits: list[str] = field(default_factory=list)

    def add(self, key: str, edit: dict[str, Any], got: VerifiedEdit | None) -> bool:
        """Count one edit against its label. Returns whether it came back passed."""
        came_back = got is not None and got.verifier_status == "passed"
        self.edits += 1
        kind = self.by_kind.setdefault(edit["kind"], {"n": 0, "passed": 0})
        kind["n"] += 1
        kind["passed"] += came_back
        if edit["label"] == "supported":
            self.supported += 1
            self.kept += came_back
        else:
            self.unsupported += 1
            self.passed += came_back
            if came_back:
                self.passed_edits.append(f"{key}#{edit['ordinal']}")
            self.reason_matched += got is not None and got.discard_reason == edit["reason"]
        if got is not None and not came_back:
            self.discarded += 1
            self.by_reason[str(got.discard_reason)] = self.by_reason.get(str(got.discard_reason), 0) + 1
        return came_back


def wilson(events: int, n: int, z: float = 1.96) -> list[float]:
    """The 95% Wilson score interval for events out of n (AI_SYSTEM 12.1), rounded."""
    if n == 0:
        return [0.0, 1.0]
    p = events / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def load_baseline() -> dict[str, Any] | None:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None


def bars_held(unsupported_passed: int, kept: int, supported: int, baseline: dict[str, Any] | None) -> dict[str, bool]:
    """The two pre-registered bars. With no baseline the second cannot hold: the gate fails closed."""
    no_drop = baseline is not None and Fraction(kept, supported) >= Fraction(
        baseline["supported_kept"], baseline["supported_total"]
    )
    return {"tailor_unsupported_passed": unsupported_passed == 0, "supported_kept_rate": bool(no_drop)}


def replay_pair(item: dict[str, Any]) -> Outcome:
    """The real path for one pair: the pipeline on the pair's facts, requirements and verdicts."""
    facts = [Fact(**row) for row in item["facts"]]
    requirements = [Requirement(r["id"], r["text"], r["importance"]) for r in item["requirements"]]
    verdicts = {r["id"]: r["verdict"] for r in item["requirements"] if r["verdict"]}
    try:
        proposal = pipeline.propose_and_verify(
            facts, requirements, verdicts, principal=None, allow_rephrase=item["allow_rephrase"]
        )
    except SchemaInvalid:  # invalid twice at the gateway: the pair fails and the run goes on
        return Outcome(None, "schema_invalid")
    return Outcome(proposal.edits, None)


def source_for(item: dict[str, Any], index: int, recorded: Path, entailment: list[Any] | None, folder: Path) -> Path:
    """The file the fake provider answers this pair from."""
    if entailment is None:
        return recorded / f"{item['key']}.json"
    answers = json.loads((RECORDINGS / f"{item['key']}.json").read_text(encoding="utf-8"))
    path = folder / f"{item['key']}.json"
    path.write_text(json.dumps([answers[0], *([entailment[index]] if entailment[index] else [])]), encoding="utf-8")
    return path


def score_pair(item: dict[str, Any], outcome: Outcome, tally: Tally) -> Result:
    """One pair against its labels. A pair is right when every supported edit came back passed and every unsupported
    one did not. If the pipeline returned a different number of edits than the pair holds (or none), no edit of it is
    counted as passed: nothing is assumed about which one is which."""
    returned = outcome.edits
    aligned = returned is not None and len(returned) == len(item["edits"])
    tally.failed_pairs += not aligned
    right, shown = aligned, []
    for position, edit in enumerate(item["edits"]):
        got = returned[position] if aligned and returned is not None else None
        came_back = tally.add(item["key"], edit, got)
        right = right and came_back == (edit["label"] == "supported")
        shown.append(
            {
                "ordinal": edit["ordinal"],
                "status": "missing" if got is None else got.verifier_status,
                "reason": None if got is None else got.discard_reason,
            }
        )
    expected = {"edits": [{"label": e["label"], "reason": e["reason"]} for e in item["edits"]]}
    actual: dict[str, Any] = {"edits": shown}
    if not aligned:
        actual["failed"] = outcome.failure or "edit_count"
    return Result(item["key"], "rephrase" if item["allow_rephrase"] else "safe_ops", expected, actual, right)


def metrics_of(tally: Tally, baseline: dict[str, Any] | None) -> dict[str, Any]:
    held = bars_held(tally.passed, tally.kept, tally.supported, baseline)
    return {
        "score": 1.0 if all(held.values()) else 0.0,  # the gate reads this: 1.0 only when both bars hold
        "tailor_unsupported_passed": tally.passed,
        "supported_kept_rate": tally.kept / tally.supported,
        "tailor_discard_rate": tally.discarded / tally.edits,
        "reason_match_rate": tally.reason_matched / tally.unsupported,
        "bars": held,
        "bars_met": all(held.values()),
        "baseline": baseline and {k: baseline[k] for k in ("supported_kept", "supported_total")},
        "intervals": {"unsupported_passed_rate": wilson(tally.passed, tally.unsupported)},
        "n_edits": tally.edits,
        "n_supported": tally.supported,
        "n_unsupported": tally.unsupported,
        "supported_kept": tally.kept,
        "unsupported_passed_edits": tally.passed_edits,
        "failed_pairs": tally.failed_pairs,
        "discarded_by_reason": dict(sorted(tally.by_reason.items())),
        "by_kind": dict(sorted(tally.by_kind.items())),
        "harness_check_only": True,  # a replay of answers built from the labels says nothing about quality
    }


def run(recorded: Path) -> Scored:
    gold = load_gold()
    entailment = json.loads(recorded.read_text(encoding="utf-8")) if recorded.is_file() else None
    if entailment is not None and len(entailment) != len(gold):
        raise ValueError("an entailment recording holds one entry per pair")
    tally, results = Tally(), []
    with tempfile.TemporaryDirectory() as folder:
        try:
            for index, item in enumerate(gold):
                provider.replay_from(source_for(item, index, recorded, entailment, Path(folder)))
                results.append(score_pair(item, replay_pair(item), tally))
        finally:
            provider.replay_from(None)
    return Scored(results, metrics_of(tally, load_baseline()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--recorded", type=Path, default=RECORDED, help="a recordings folder (default) or an entailment recording"
    )
    args = parser.parse_args(argv)
    if why := scratch.refusal(live=False):  # a recorded run is frozen for good: only a scratch database may take it
        parser.error(why)
    try:
        done = runner.run_suite(SUITE, args.recorded)
    except runner.ReplayNeedsFake:
        parser.error("a replay needs LLM_PROVIDER=fake; this command never calls a real model")
    print(f"{SUITE}: {done.passed} of {done.n} pairs passed, status {done.status}", file=sys.stderr)
    print(done.run_id)  # the last line: the gate takes it
    return 2 if done.status == "error" else 0


if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = "fake"  # a replay needs no model: the answers come from a recorded file
    config.get.cache_clear()
    sys.exit(main())
