"""The evidence_faithfulness suite (plan 04-02; AI_SYSTEM 4.4 and 12.2): replay a recording through the real request
builder, the gateway's fake provider and the citation validator, and score the verdicts against the gold labels.

The Phase 1 runner calls run() (this suite is an OwnScorer entry in backend/app/evals/runner.py), so a replay is one
eval_runs row with one eval_items row per pair, gated like any other suite:

    cd backend && uv run python -m app.evals run evidence_faithfulness
    cd backend && uv run python -m app.evals gate evidence_faithfulness --run <the id printed last>
    cd backend && uv run python ../evals/evidence_faithfulness/score.py [--recorded FILE]   # the same, recorded

Every pair goes through the real path: build_request, the gateway (a ledger row per call), validate, and one fresh call
when the validator rejects the first answer (AI_SYSTEM 4.4). In a replay only the model is replaced: the answers come
from recorded.json, which was built from the gold labels. So the score proves that the code path works and that a
damaged recording fails; it is NOT a model quality number (invariant 8). README.md holds the pre-registered bars.

The same path runs live, with the model the environment configures answering every pair (run(None); LIVE below):

    cd backend && uv run python -m app.evals run evidence_faithfulness --live

That run records method "model" and the model's name beside the prompt version, which is what the match route gate
(app.match.ai_gate) needs, and its score is a measurement of the model against the bars in the README.
"""

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from app.evals import runner, scratch
from app.evals.runner import Result, Scored
from app.match import evidence, validator
from app.platform import config
from app.platform.llm import SchemaInvalid
from app.platform.llm import fake as provider

HERE = Path(__file__).parent
GOLD = HERE / "gold.jsonl"
RECORDED = HERE / "recorded.json"  # the runner's default recording for this suite
POSTINGS = HERE.parent / "requirement_extraction" / "gold.jsonl"  # where each pair's posting text lives
SUITE = "evidence_faithfulness"
TASK = evidence.TASK  # the task whose route the run's record names
LABELS = "agent-drafted, maintainer review pending"
CONFIG = {"labels": LABELS, "prompt_version": evidence.PROMPT_VERSION, "gold": GOLD.name}
LIVE = True  # run(None) calls the real model: the runner's --live (backend/app/evals/runner.py) needs this to allow it
# The bars of README.md "Pre-registered bars", written before any run (AI_SYSTEM 4.4 targets and its 2% alert line).
FAITHFUL_BAR, FALSE_MET_BAR, REJECT_BAR = 0.90, 0.05, 0.02
ATTEMPTS = 2  # the first answer and one fresh call after a rejection
UNSUPPORTED = ("not_met", "no_evidence")  # gold verdicts that a "met" on a required item contradicts


class Outcome(NamedTuple):
    """What one pair came to: an accepted verdict set (and its request), or why it was not accepted."""

    accepted: validator.Accepted | None
    request: evidence.EvidenceRequest
    tries: int
    failure: str | None  # None when accepted; "schema_invalid" or "rule <n>: <code>"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_gold() -> list[dict[str, Any]]:
    items = read_jsonl(GOLD)
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("gold.jsonl has a repeated key")
    return items


def wilson(correct: int, n: int, z: float = 1.96) -> list[float]:
    """The 95% Wilson score interval for correct out of n (AI_SYSTEM 12.1), rounded."""
    if n == 0:
        return [0.0, 1.0]
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def ratio(top: int, bottom: int) -> float:
    return top / bottom if bottom else 0.0


def bars_held(faithful: float, false_met: float, rejected: float) -> dict[str, bool]:
    return {
        "evidence_faithful_rate": faithful >= FAITHFUL_BAR,
        "false_met_required_rate": false_met <= FALSE_MET_BAR,
        "citation_reject_rate": rejected <= REJECT_BAR,
    }


def replay_pair(item: dict[str, Any], description: str) -> Outcome:
    """The real path for one pair: request, gateway, validator, and one fresh call when the validator rejects."""
    request = evidence.build_request(
        item["facts"], item["requirements"], description=description, today=date.fromisoformat(item["as_of"])
    )
    failure = None
    for tries in range(1, ATTEMPTS + 1):
        try:
            answer = evidence.score(request, principal=None)
        except SchemaInvalid:  # invalid at the gateway twice: the pair fails and the run goes on
            return Outcome(None, request, tries, "schema_invalid")
        result = validator.validate(answer, request, career_months=request.career_months)
        if isinstance(result, validator.Accepted):
            return Outcome(result, request, tries, None)
        failure = f"rule {result.rule}: {result.code}"
    return Outcome(None, request, ATTEMPTS, failure)


def run(recorded: Path | None) -> Scored:
    """Score every pair. `recorded` is a recording to replay through the gateway's fake provider; None is a live run,
    where the gateway calls the model the environment configures and nothing is replayed."""
    gold = load_gold()
    descriptions = {row["posting_id"]: row["description"] for row in read_jsonl(POSTINGS)}
    results: list[Result] = []
    count = dict.fromkeys(
        (
            "accepted",
            "rejected",
            "schema_invalid",
            "retried",
            "weak_fit",
            "verdicts",
            "faithful",
            "required",
            "false_met",
        ),
        0,
    )
    if recorded is not None:
        provider.replay_from(recorded)  # the gateway's fake provider hands these out in the order the calls are made
    try:
        for item in gold:
            outcome = replay_pair(item, descriptions[item["posting_ref"]])
            wanted = {v["requirement"]: v["verdict"] for v in item["expected"]["verdicts"]}
            got: dict[str, str] = {}
            if outcome.accepted is not None:
                alias_of = {ref.id: alias for alias, ref in outcome.request.requirements.items()}
                got = {alias_of[row.requirement_id]: row.verdict for row in outcome.accepted.rows}
                count["accepted"] += 1
                count["weak_fit"] += outcome.accepted.weak_fit
                count["verdicts"] += len(got)
                count["faithful"] += sum(got[alias] == wanted[alias] for alias in got)
                for row in outcome.accepted.rows:
                    count["required"] += row.importance == "required"
                    count["false_met"] += (
                        row.importance == "required"
                        and row.verdict == "met"
                        and wanted[alias_of[row.requirement_id]] in UNSUPPORTED
                    )
            elif outcome.failure == "schema_invalid":
                count["schema_invalid"] += 1
            else:
                count["rejected"] += 1
            count["retried"] += outcome.tries > 1
            actual: dict[str, Any] = (
                {"verdicts": got, "tries": outcome.tries}
                if outcome.accepted is not None
                else {"failed": outcome.failure, "tries": outcome.tries}
            )
            results.append(Result(item["key"], item["slice"], {"verdicts": wanted}, actual, got == wanted))
    finally:
        provider.replay_from(None)
    faithful = ratio(count["faithful"], count["verdicts"])
    false_met = ratio(count["false_met"], count["required"])
    rejected = ratio(count["rejected"], len(gold))
    held = bars_held(faithful, false_met, rejected)
    metrics: dict[str, Any] = {
        "score": 1.0 if all(held.values()) else 0.0,  # the gate reads this: 1.0 only when every bar holds
        "evidence_faithful_rate": faithful,
        "false_met_required_rate": false_met,
        "citation_reject_rate": rejected,
        "bars": held,
        "bars_met": all(held.values()),
        "intervals": {
            "evidence_faithful_rate": wilson(count["faithful"], count["verdicts"]),
            "false_met_required_rate": wilson(count["false_met"], count["required"]),
            "citation_reject_rate": wilson(count["rejected"], len(gold)),
        },
        "pairs": len(gold),
        "accepted_pairs": count["accepted"],
        "rejected_pairs": count["rejected"],
        "schema_invalid_pairs": count["schema_invalid"],
        "retried_pairs": count["retried"],
        "weak_fit_pairs": count["weak_fit"],
        "verdicts": count["verdicts"],
        "faithful_verdicts": count["faithful"],
        "required_items": count["required"],
        "false_met_required": count["false_met"],
        "harness_check_only": recorded
        is not None,  # a replay of answers built from the gold says nothing about quality
    }
    return Scored(results, metrics)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--recorded", type=Path, default=RECORDED, help="the recording to replay (default recorded.json)"
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
