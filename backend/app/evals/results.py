"""Results files (EVL-05): the only way a quality figure leaves the database.

`export(run_id, suite, out_dir)` writes `<out_dir>/<suite>.json` from a finished recorded run, with its run id and the
commit it ran on, so a number in a file or in the README can always be traced to a run. Nothing is computed here: every
value is the stored one. A run that is still running or ended in an error has no result, a run of another suite is not
the suite asked for, and a harness suite proves the runner, not the product, so each is refused and nothing is written.

A replay answers from recordings, so its file says so in `note`: a wiring check, not a quality measure.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.platform import db

from . import EVALS, HARNESS_ONLY

FORMAT = 1
RESULTS = EVALS / "results"
REPLAY_NOTE = "Replay of recorded model answers: a wiring check, not a quality measure."
LIVE_NOTE = "Live run against a real model, recorded on the commit named here."


class ExportRefused(Exception):
    """The run is not one that may leave the database as a result. The message says why."""


def export(run_id: UUID, suite: str, out_dir: Path = RESULTS) -> Path:
    """Write the run's results file and return its path; ExportRefused (nothing written) when it may not leave."""
    if suite in HARNESS_ONLY:
        raise ExportRefused(f"{suite} is a harness check: it proves the runner and the gate, not the product")
    with db.transaction(role="worker") as conn:
        run = (
            conn.execute(
                text(
                    """SELECT suite::text AS suite, git_sha, status::text AS status, config, metrics,
                              started_at, finished_at
                         FROM eval_runs WHERE id = :id"""
                ),
                {"id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    if run is None:
        raise ExportRefused(f"run {run_id} does not exist")
    if run["suite"] != suite:
        raise ExportRefused(f"run {run_id} belongs to suite {run['suite']}, not {suite}")
    if run["status"] not in ("passed", "failed"):
        raise ExportRefused(f"run {run_id} has status {run['status']}: only a finished run has a result")
    metrics: dict[str, Any] = run["metrics"]
    config: dict[str, Any] = run["config"]
    mode = str(config.get("mode", "replay"))
    document = {
        "format": FORMAT,
        "suite": suite,
        "run_id": str(run_id),
        "status": run["status"],
        "mode": mode,
        "git_sha": run["git_sha"],
        "started_at": run["started_at"].isoformat(),
        "finished_at": run["finished_at"].isoformat(),
        "n": metrics.get("n"),
        "passed": metrics.get("passed"),
        "score": metrics.get("score"),
        "metrics": metrics,
        "labels": config.get("labels"),
        "note": REPLAY_NOTE if mode == "replay" else LIVE_NOTE,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{suite}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
