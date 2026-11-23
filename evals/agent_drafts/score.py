"""The agent_drafts suite (plan 07-02; CMP-03, CMP-04, CMP-05, AI_SYSTEM 6.6, 6.8, 6.9 and 12): replay 10 Writer packets,
10 Chaser drafts and 30 Coach prep packs through the real request builders, the gateway's fake provider and the real
validators (packet.check_draft and assemble, chaser.validate, coach.validate, with layer 1 and one verify.entail call),
and score what is kept against the labels.

The Phase 1 runner calls run() (this suite is an OwnScorer entry in backend/app/evals/runner.py), so a replay is one
eval_runs row with one eval_items row per item, gated like any other suite:

    cd backend && uv run python -m app.evals run agent_drafts
    cd backend && uv run python -m app.evals gate agent_drafts --run <the id printed last>
    cd backend && uv run python ../evals/agent_drafts/score.py [--recorded FILE]       # the same, recorded

The default recording is the folder recorded/: one file per item, the model's draft and then the verify.entail answer for
the sentences that reach layer 2. `--recorded degraded/pass_through.json` switches every validator to pass-through for the
run (layer 1, verify.entail, the restricted-content and citation tests): that flag exists only here, in the scorer, and
the run must fail the gate. The score proves that the code path works and that a fault fails; it is NOT a model quality
number (invariant 8). README.md holds the pre-registered bars.
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.apply import packet
from app.campaigns.crew import chaser, coach, common
from app.evals import runner, scratch
from app.evals.runner import Result, Scored
from app.platform import config
from app.platform.llm import complete
from app.platform.llm import fake as provider
from app.tailor import api as tailor

HERE = Path(__file__).parent
GOLD = HERE / "gold.jsonl"
RECORDINGS = HERE / "recorded"
RECORDED = RECORDINGS  # the runner's default recording for this suite: a folder, one file per item
SUITE = "agent_drafts"
TASK = chaser.TASK  # the task whose route the run's record names
LABELS = "agent-drafted, maintainer review pending"
BARS = {
    "drafts_unsupported_passed": 0,
    "drafts_supported_kept_rate": 0.90,
    "drafts_candidate_only_blank": 1.0,
    "prep_items_without_fact": 0,
    "drafts_restricted_passed": 0,
}
CANDIDATE_ONLY = ("work_authorization", "expected_pay", "notice_period", "start_date")
# The scorer's own test for restricted content, written here and not borrowed from the code under test.
RESTRICTED = re.compile(
    r"@|https?://|www\.|\+?\d[\d\s().-]{7,}\d|\b(salary|notice|availab\w*|start date|compensation)\b", re.IGNORECASE
)


def load_gold() -> list[dict[str, Any]]:
    items = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("gold.jsonl has a repeated key")
    return items


CONFIG = {
    "labels": LABELS,
    "prompt_versions": {
        packet.TASK: packet.PROMPT_VERSION,
        chaser.TASK: chaser.PROMPT_VERSION,
        coach.TASK: coach.PROMPT_VERSION,
    },
    "gold": GOLD.name,
}


@contextmanager
def pass_through() -> Iterator[None]:
    """Every validator says yes: the fault the degraded run replays. Patches the names the code under test calls."""
    saved = (tailor.unsupported_reason, tailor.entailed, common.restricted_reason, common.needs_citation)
    tailor.unsupported_reason = lambda *a, **k: None  # type: ignore[assignment]
    tailor.entailed = lambda items, **k: [True] * len(list(items))  # type: ignore[assignment]
    common.restricted_reason = lambda text: None  # type: ignore[assignment]
    common.needs_citation = lambda *a, **k: False  # type: ignore[assignment]
    try:
        yield
    finally:
        tailor.unsupported_reason, tailor.entailed, common.restricted_reason, common.needs_citation = saved


class Tally:
    def __init__(self) -> None:
        self.supported = self.kept = self.unsupported_passed = self.restricted_passed = 0
        self.writers = self.writers_blank = self.without_fact = 0
        self.reasons_expected = self.reasons_seen = 0
        self.passed_unsupported: list[str] = []

    def label(self, key: str, text: str, label: str, kept: bool) -> bool:
        """Count one labelled sentence. True when what came back agrees with the label."""
        if label == "supported":
            self.supported += 1
            self.kept += kept
        elif kept:
            self.unsupported_passed += 1
            self.passed_unsupported.append(f"{key}: {text[:40]}")
        if kept and RESTRICTED.search(text):
            self.restricted_passed += 1
        return kept == (label == "supported")

    def reason(self, expected: str | None, removed: dict[str, int]) -> None:
        if expected:
            self.reasons_expected += 1
            self.reasons_seen += expected in removed


def facts_of(item: dict[str, Any]) -> list[tailor.Fact]:
    return [tailor.Fact(**row) for row in item["facts"]]


def skills_of(facts: list[tailor.Fact]) -> list[str]:
    return [str(f.attributes.get("name") or f.statement) for f in facts if f.kind == "skill"]


def score_writer(item: dict[str, Any], tally: Tally) -> Result:
    facts, requirements = facts_of(item), item["requirements"]
    claims = [{"text": text} for text in item["dossier"]] or None
    answer = complete(
        packet.TASK,
        packet.PacketDraft,
        packet.build_messages(facts, requirements, dossier_claims=claims),
        principal=None,
        prompt_version=packet.PROMPT_VERSION,
    )
    checked, note = packet.check_draft(answer, facts, requirements, principal=None, dossier_claims=claims)  # type: ignore[arg-type]
    answers, _, _ = packet.assemble(checked, note, facts, with_claims=bool(claims))
    kept = {group: {s.text for s in got.sentences} for group, got in checked.items()}
    kept["note"] = {s.text for s in note.sentences}
    right = [
        tally.label(item["key"], s["text"], s["label"], s["text"] in kept.get(s["group"], set()))
        for s in item["sentences"]
    ]
    blank = all(
        a["status"] == "candidate_must_answer" and not a["answer"]
        for a in answers
        if a["question_id"] in CANDIDATE_ONLY
    )
    tally.writers += 1
    tally.writers_blank += blank
    expected = {
        "supported": [s["text"] for s in item["sentences"] if s["label"] == "supported"],
        "candidate_only_blank": True,
    }
    actual = {"kept": sorted(t for group in kept.values() for t in group), "candidate_only_blank": blank}
    return Result(item["key"], "writer", expected, actual, all(right) and blank)


def score_chaser(item: dict[str, Any], tally: Tally) -> Result:
    facts = facts_of(item)
    message = chaser.draft(facts, item["dossier"], item["events"], item["requirements"], principal=None)
    kept, removed = chaser.validate(
        message,
        facts=facts,
        claims=item["dossier"],
        events=item["events"],
        skills=skills_of(facts),
        job_terms=item["requirements"],
        principal=None,
    )
    kept_texts = {s.text for s in kept}
    right = [tally.label(item["key"], s["text"], s["label"], s["text"] in kept_texts) for s in item["sentences"]]
    for s in item["sentences"]:
        if s["label"] == "unsupported":
            tally.reason(s["reason"], removed)
    expected = {"supported": [s["text"] for s in item["sentences"] if s["label"] == "supported"]}
    return Result(
        item["key"], "chaser", expected, {"kept": sorted(kept_texts), "removed_by_reason": removed}, all(right)
    )


def score_coach(item: dict[str, Any], tally: Tally) -> Result:
    facts = facts_of(item)
    requirements = [
        tailor.Requirement(100 + n, r["text"], r["importance"]) for n, r in enumerate(item["requirements"], 1)
    ]
    verdicts = {req.id: row["verdict"] for req, row in zip(requirements, item["requirements"], strict=True)}
    pack = coach.draft(facts, requirements, verdicts, item["dossier"], principal=None)
    out, removed = coach.validate(
        pack,
        facts=facts,
        requirements=requirements,
        verdicts=verdicts,
        claims=item["dossier"],
        skills=skills_of(facts),
        job_terms=[r.text for r in requirements],
        principal=None,
    )
    kept = {q.text for q in out.questions} | {a.text for a in out.ask_them}
    labelled = [*item["questions"], *item["ask_them"]]
    right = [tally.label(item["key"], q["text"], q["label"], q["text"] in kept) for q in labelled]
    tally.without_fact += sum(not q.gap and not q.facts for q in out.questions)
    tally.without_fact += sum(not a.dossier_claims for a in out.ask_them)
    for q in labelled:
        if q["label"] == "unsupported":
            tally.reason(q["reason"], removed)
    expected = {"supported": [q["text"] for q in labelled if q["label"] == "supported"]}
    return Result(item["key"], "coach", expected, {"kept": sorted(kept), "removed_by_reason": removed}, all(right))


SCORERS: dict[str, Callable[[dict[str, Any], Tally], Result]] = {
    "writer": score_writer,
    "chaser": score_chaser,
    "coach": score_coach,
}


def metrics_of(tally: Tally) -> dict[str, Any]:
    measured = {
        "drafts_unsupported_passed": tally.unsupported_passed,
        "drafts_supported_kept_rate": tally.kept / tally.supported,
        "drafts_candidate_only_blank": tally.writers_blank / tally.writers,
        "prep_items_without_fact": tally.without_fact,
        "drafts_restricted_passed": tally.restricted_passed,
    }
    held = {
        "drafts_unsupported_passed": measured["drafts_unsupported_passed"] <= BARS["drafts_unsupported_passed"],
        "drafts_supported_kept_rate": measured["drafts_supported_kept_rate"] >= BARS["drafts_supported_kept_rate"],
        "drafts_candidate_only_blank": measured["drafts_candidate_only_blank"] >= BARS["drafts_candidate_only_blank"],
        "prep_items_without_fact": measured["prep_items_without_fact"] <= BARS["prep_items_without_fact"],
        "drafts_restricted_passed": measured["drafts_restricted_passed"] <= BARS["drafts_restricted_passed"],
    }
    return {
        "score": 1.0 if all(held.values()) else 0.0,  # the gate reads this: 1.0 only when every bar holds
        **measured,
        "bars": held,
        "bars_met": all(held.values()),
        "reason_match_rate": tally.reasons_seen / tally.reasons_expected,
        "n_supported": tally.supported,
        "supported_kept": tally.kept,
        "unsupported_passed_items": tally.passed_unsupported[:20],
        "harness_check_only": True,  # a replay of answers built from the labels says nothing about quality
    }


def run(recorded: Path) -> Scored:
    gold = load_gold()
    faulty = recorded.is_file()  # the degraded fixture is one file: every validator switched to pass-through
    folder = RECORDINGS if faulty else recorded
    tally, results = Tally(), []
    try:
        with pass_through() if faulty else _nothing():
            for item in gold:
                provider.replay_from(folder / f"{item['key']}.json")
                results.append(SCORERS[item["kind"]](item, tally))
    finally:
        provider.replay_from(None)
    return Scored(results, metrics_of(tally))


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--recorded", type=Path, default=RECORDED, help="a recordings folder (default) or the degraded file"
    )
    args = parser.parse_args(argv)
    if why := scratch.refusal(live=False):  # a recorded run is frozen for good: only a scratch database may take it
        parser.error(why)
    try:
        done = runner.run_suite(SUITE, args.recorded)
    except runner.ReplayNeedsFake:
        parser.error("a replay needs LLM_PROVIDER=fake; this command never calls a real model")
    print(f"{SUITE}: {done.passed} of {done.n} items passed, status {done.status}", file=sys.stderr)
    print(done.run_id)  # the last line: the gate takes it
    return 2 if done.status == "error" else 0


if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = "fake"  # a replay needs no model: the answers come from a recorded file
    config.get.cache_clear()
    sys.exit(main())
