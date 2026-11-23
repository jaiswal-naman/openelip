"""Write one recorded run's summary to runs/<run id>.json (05 fix A5).

    cd backend && uv run python ../evals/tailoring_integrity/export_run.py <run id> --kind clean|degraded_entail|verifier_check_off

A recorded run lives in the eval_runs table of a scratch database, which is dropped afterwards, so the numbers the README
cites are kept here: metrics, counts, prompt versions and the git commit the run was made at. Nothing in a run row holds
the text of a resume or of an edit (only ids such as "ti-003#2" and counts), and this script copies nothing else.
Refuses a database whose name is not `..._test` or `evals`.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

RUNS = Path(__file__).parent / "runs"
KINDS = ("clean", "degraded_entail", "verifier_check_off")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--kind", choices=KINDS, required=True)
    args = parser.parse_args()
    url = make_url(os.environ["DATABASE_URL_MIGRATE"])
    if not (url.database or "").endswith("_test") and url.database != "evals":
        print("refusing: the database is not a scratch database", file=sys.stderr)
        return 2
    with create_engine(url).connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, suite::text AS suite, git_sha, status::text AS status, is_gate_passed, config, metrics, "
                "started_at FROM eval_runs WHERE id = :id"
            ),
            {"id": args.run_id},
        ).one()
    config = row.config
    summary = {
        "run_id": str(row.id),
        "suite": row.suite,
        "kind": args.kind,
        "git_sha": row.git_sha,
        "date": row.started_at.date().isoformat(),
        "status": row.status,
        "is_gate_passed": row.is_gate_passed,
        "recorded": config.get("recorded"),
        "mode": config.get("mode"),
        "provider": config.get("provider"),
        "labels": config.get("labels"),
        "prompt_versions": config.get("prompt_versions"),
        "metrics": row.metrics,
    }
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{row.id}.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
