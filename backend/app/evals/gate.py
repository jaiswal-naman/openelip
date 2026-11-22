"""The regression gate: a recorded run against the floor in evals/thresholds.toml (requirement PLT-08).

The gate reads only what a run recorded (eval_runs and eval_items), never a fresh model answer. It fails closed: a suite
with no floor, or a run that is missing, unfinished, ended in an error or belongs to another suite, is a failure and
never a pass. Exit code 0 means the run meets its floor, 1 means it does not.
"""

import tomllib
from uuid import UUID

from sqlalchemy import text

from app.platform import db

from . import EVALS, HARNESS_ONLY

HARNESS_NOTE = "harness check only: this suite proves the runner and the gate, not product quality"


def floor(suite: str) -> float | None:
    """The lowest score the suite may reach, or None when thresholds.toml has no entry for it."""
    with (EVALS / "thresholds.toml").open("rb") as handle:
        entry = tomllib.load(handle).get(suite)
    return None if entry is None else float(entry["min_score"])


def check(run_id: UUID, suite: str) -> tuple[bool, list[str]]:
    """(did it pass, the lines to print). Item keys are ids chosen by the people who wrote the gold set."""
    with db.transaction(role="worker") as conn:
        run = (
            conn.execute(
                text("SELECT suite::text AS suite, status::text AS status, metrics FROM eval_runs WHERE id = :id"),
                {"id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        failed = [
            row.item_key
            for row in conn.execute(
                text("SELECT item_key FROM eval_items WHERE eval_run_id = :id AND NOT is_passed ORDER BY id"),
                {"id": run_id},
            )
        ]
    head = f"gate {suite}: run {run_id}"
    if run is None:
        return False, [f"{head} does not exist: FAIL"]
    if run["suite"] != suite:
        return False, [f"{head} belongs to suite {run['suite']}, not {suite}: FAIL"]
    if run["status"] in ("running", "error"):
        return False, [f"{head} has status {run['status']} and did not finish cleanly: FAIL"]
    bar = floor(suite)
    if bar is None:
        return False, [f"{head} cannot pass: evals/thresholds.toml has no floor for {suite}: FAIL"]
    score = float(run["metrics"]["score"])
    passed = score >= bar
    metrics = run["metrics"]
    lines = [
        f"{head} scored {score:.3f} ({metrics['passed']} of {metrics['n']} items), floor {bar:.3f}: "
        f"{'PASS' if passed else 'FAIL'}"
    ]
    if failed:
        lines.append("items that failed: " + ", ".join(failed))
    if suite in HARNESS_ONLY:
        lines.append(HARNESS_NOTE)
    return passed, lines


def main(suite: str, run_id: UUID) -> int:
    passed, lines = check(run_id, suite)
    print("\n".join(lines))
    return 0 if passed else 1
