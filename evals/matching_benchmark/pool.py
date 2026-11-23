"""Build pool.jsonl: for every profile, the union of the first 10 jobs of M1 to M4 (README "How the pairs to judge are
chosen").

    cd backend
    uv run python ../evals/matching_benchmark/pool.py --database-url postgresql://app:...@127.0.0.1:5433/<name>_test

A pair is written as {profile_key, job_ref, methods}, with the posting's stable ref and not a database id, so the pool
survives a reload of the benchmark database. The methods that returned the job are kept for the report; cards.py leaves
them out, so a labeller never learns which method found a job.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import target  # noqa: E402

POOL = HERE / "pool.jsonl"
META = HERE / "snapshot.meta.json"
TOP = 10  # jobs each method adds to the pool


def pool_for(conn: Connection, user_id: UUID, refs: dict[int, str], now: datetime) -> dict[str, list[str]]:
    """Job ref to the methods whose first TOP jobs hold it, for one person."""
    import methods

    lists = {
        "M1": methods.keyword(conn, user_id),
        "M2": methods.vector(conn, user_id),
        "M3": methods.hybrid(conn, user_id),
        "M4": methods.hybrid_rules(conn, user_id, now),
    }
    found: dict[str, list[str]] = {}
    for name, jobs in lists.items():
        for job in jobs[:TOP]:
            found.setdefault(refs[job.job_id], []).append(name)
    return found


def build(database_url: str) -> list[dict[str, Any]]:
    url = target.require_scratch(database_url)
    target.point_app_at(url)
    from app.platform import db

    now = datetime.fromisoformat(json.loads(META.read_text(encoding="utf-8"))["exported_at"])
    with db.transaction(role="migrate") as admin:
        target.require_head(admin)
        refs, people = target.job_refs(admin), target.profile_ids(admin)
    rows = []
    for key in sorted(people):
        with db.transaction(user_id=people[key], role="match") as conn:
            for ref, names in sorted(pool_for(conn, people[key], refs, now).items()):
                rows.append({"profile_key": key, "job_ref": ref, "methods": names})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", required=True, help="the benchmark database; its name must end in _test")
    args = ap.parse_args()
    rows = build(args.database_url)
    POOL.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")
    print(f"{len(rows)} pairs for {len({r['profile_key'] for r in rows})} profiles written to {POOL.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
