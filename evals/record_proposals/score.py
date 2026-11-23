"""The record_proposals suite (plan 05-06; CHT-03, AI_SYSTEM 4.8): 20 invented candidate messages, each sent through
app.chat.api.propose with a recorded record.propose answer, scored against the proposals a careful reader expects.

    cd backend && uv run python -m app.evals run record_proposals
    cd backend && uv run python -m app.evals gate record_proposals --run <the id printed last>

Every script starts from a freshly seeded world (evals/chat/seed.py; the seed refuses any database whose name does not
end in _test), so no script can leave a declined or stored proposal for the next. README.md holds the pre-registered
bars; the score proves the checks and the wiring, and is NOT a model quality number (invariant 8).
"""

import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.chat import api as chat
from app.chat import proposals
from app.evals.runner import Result, Scored
from app.platform.llm import fake as provider

HERE = Path(__file__).parent


def _turns() -> Any:
    key = "evals_chat_turns"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE.parent / "chat" / "turns.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


turns = _turns()
seed = turns.seed

GOLD = HERE / "gold.jsonl"
RECORDINGS = HERE / "recordings"
RECORDED = RECORDINGS
BASELINE = HERE / "baseline.json"
SUITE = "record_proposals"
TASK = proposals.TASK
LABELS = "agent-drafted, maintainer review pending"


def load_gold() -> list[dict[str, Any]]:
    items = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({item["key"] for item in items}) != len(items):
        raise ValueError("gold.jsonl has a repeated key")
    return items


CONFIG = {
    "labels": LABELS,
    "prompt_versions": {proposals.TASK: proposals.PROMPT_VERSION},
    "gold": GOLD.name,
    "attempts": 1,
}

DECLINE = text(
    """WITH asked AS (
         INSERT INTO profile_events (user_id, event_type, entity, operation, after, origin, created_at)
         VALUES (:u, 'proposed', :entity, :operation, CAST(:after AS jsonb), 'chat', now() - make_interval(days => :days))
         RETURNING id)
       INSERT INTO profile_events (user_id, event_type, entity, operation, after, proposal_event_id, created_at)
       SELECT :u, 'declined', :entity, :operation, CAST(:after AS jsonb), id, now() - make_interval(days => :days)
         FROM asked"""
)


def decline(conn: Connection, person: Any, declined: dict[str, Any]) -> None:
    """A proposal the candidate answered with no `days` days ago, as the record's change log would hold it."""
    conn.execute(
        DECLINE,
        {
            "u": person,
            "entity": declined["entity"],
            "operation": declined["operation"],
            "after": json.dumps(declined["after"]),
            "days": declined["days"],
        },
    )


def norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    if isinstance(value, list):
        return sorted(norm(v) for v in value)
    return value


def is_expected(expected: dict[str, Any], event: Any) -> bool:
    """A stored proposal is an expected one when entity and operation agree and every part named of the row it writes
    says the same (case and spacing aside). An expected `after` of None means a removal that writes nothing."""
    if (event.entity, event.operation) != (expected["entity"], expected["operation"]):
        return False
    if expected["after"] is None:
        return event.after is None
    return event.after is not None and all(
        norm(event.after.get(name)) == norm(value) for name, value in expected["after"].items()
    )


def pair_up(expected: list[dict[str, Any]], stored: list[Any]) -> int:
    """How many stored proposals matched an expected one, each used once."""
    free, matched = list(expected), 0
    for event in stored:
        found = next((e for e in free if is_expected(e, event)), None)
        if found is not None:
            free.remove(found)
            matched += 1
    return matched


def load_baseline() -> dict[str, Any] | None:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else None


def run(recorded: Path) -> Scored:
    gold = load_gold()
    engine = seed.open_database()
    results: list[Result] = []
    tally = {"stored": 0, "expected": 0, "matched": 0, "writes": 0, "ok": 0}
    try:
        for item in gold:
            facts = [(f"extra{n}", kind, statement) for n, (kind, statement) in enumerate(item["facts"])]
            with engine.begin() as conn:
                world = seed.seed(conn, extra_facts=facts)
                for declined in item["declined"]:
                    decline(conn, world.a, declined)
            before = turns.state(engine, world.a)
            provider.replay_from(recorded / f"{item['key']}.json")
            try:
                stored = chat.propose(world.a, item["message"], principal=world.a)
            finally:
                provider.replay_from(None)
            after = turns.state(engine, world.a)
            moved = [name for name in turns.MUST_NOT_MOVE if before[name] != after[name]]
            rows = after["proposed_events"] - before["proposed_events"]
            matched = pair_up(item["expect"], stored)
            tally["stored"] += len(stored)
            tally["expected"] += len(item["expect"])
            tally["matched"] += matched
            tally["writes"] += bool(moved)
            right = not moved and rows == len(stored) and matched == len(stored) == len(item["expect"])
            tally["ok"] += right
            results.append(
                Result(
                    item["key"],
                    "trap" if item["case"].startswith("trap") else "proposal",
                    {"expect": len(item["expect"]), "case": item["case"]},
                    {"stored": len(stored), "matched": matched, "moved": moved, "rows": rows},
                    right,
                )
            )
    finally:
        engine.dispose()
    return Scored(results, metrics_of(tally, len(gold), load_baseline()))


def bars_held(metrics: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, bool]:
    """The pre-registered bars. With no baseline the two relative bars cannot hold: the gate fails closed."""

    def not_below(name: str) -> bool:
        return baseline is not None and Fraction(str(metrics[name])) >= Fraction(str(baseline[name]))

    return {
        "unconfirmed_writes": metrics["unconfirmed_writes"] == 0,
        "proposal_precision": not_below("proposal_precision"),
        "proposal_recall": not_below("proposal_recall"),
    }


def metrics_of(tally: dict[str, int], n: int, baseline: dict[str, Any] | None) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "proposal_precision": tally["matched"] / tally["stored"] if tally["stored"] else 0.0,
        "proposal_recall": tally["matched"] / tally["expected"] if tally["expected"] else 0.0,
        "unconfirmed_writes": tally["writes"],
        "scripts_right": tally["ok"],
        "n_stored": tally["stored"],
        "n_expected": tally["expected"],
        "attempts": 1,
        "n": n,
        "harness_check_only": True,
    }
    held = bars_held(metrics, baseline)
    return {
        "score": 1.0 if all(held.values()) else 0.0,
        **metrics,
        "bars": held,
        "baseline": baseline and {k: baseline[k] for k in ("proposal_precision", "proposal_recall")},
    }
