"""Draw the requirement-extraction gold postings from the index and write an unlabelled postings.jsonl.

    DATABASE_URL=... uv run --project backend python evals/requirement_extraction/build_sample.py

Reads the open canonical postings the index holds, in a read-only session (SELECT only), leaves out postings over
10,000 characters and postings that contain an email address or phone number, and draws 50 with at least 10 per ATS.
It refuses to overwrite an existing postings.jsonl: the set is frozen, and a second draw would drop the labels.
The labels are written by hand in labels.jsonl, and resolve.py turns them into character spans.
"""

import json
import os
import random
import re
import sys
from pathlib import Path

POSTINGS = Path(__file__).with_name("postings.jsonl")
SEED = 20261122
SIZE = 50
DEV = 10
MIN_PER_ATS = 10
MAX_CHARS = 10_000

# The same two patterns are in evals/location/build_sample.py.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}\b")

POOL_SQL = """
SELECT s.ats, s.token, j.external_id, j.title, j.description
FROM jobs j JOIN job_sources s ON s.id = j.source_id
WHERE j.status = 'open' AND j.canonical_job_id IS NULL
"""


def has_contact_data(*texts: str | None) -> bool:
    return any(t and (EMAIL.search(t) or PHONE.search(t)) for t in texts)


def read_pool(database_url: str) -> tuple[list[dict], dict[str, int]]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        conn.read_only = True  # the server refuses any write on this connection
        rows = conn.execute(POOL_SQL).fetchall()
    counts = {"open canonical": len(rows), "too long": 0, "contact data": 0}
    pool = []
    for ats, board, external_id, title, description in rows:
        if len(description) > MAX_CHARS:
            counts["too long"] += 1
        elif has_contact_data(description, title):
            counts["contact data"] += 1
        else:
            pool.append(
                {"ats": ats, "board": board, "external_id": external_id, "title": title, "description": description}
            )
    pool.sort(key=lambda p: (p["ats"], p["board"], p["external_id"]))  # same draw whatever order the database answers
    return pool, counts


def draw(pool: list[dict]) -> list[dict]:
    """At least MIN_PER_ATS from each ATS, the rest from what is left, then shuffled; the first DEV rows are dev."""
    rng = random.Random(SEED)
    by_ats: dict[str, list[int]] = {}
    for i, p in enumerate(pool):
        by_ats.setdefault(p["ats"], []).append(i)
    picked = [i for ats in sorted(by_ats) for i in rng.sample(by_ats[ats], MIN_PER_ATS)]
    taken = set(picked)
    picked += rng.sample([i for i in range(len(pool)) if i not in taken], SIZE - len(picked))
    rng.shuffle(picked)
    return [{"id": f"req-{n:03d}", "split": "dev" if n <= DEV else "test", **pool[i]} for n, i in enumerate(picked, 1)]


def main() -> int:
    if POSTINGS.exists():
        print(f"{POSTINGS.name} exists and is frozen; refusing to overwrite it", file=sys.stderr)
        return 1
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("set DATABASE_URL (read-only use)", file=sys.stderr)
        return 1
    pool, counts = read_pool(database_url)
    rows = draw(pool)
    with POSTINGS.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{counts}; pool {len(pool)}")
    for ats in sorted({p["ats"] for p in pool}):
        print(
            f"{ats}: pool {sum(p['ats'] == ats for p in pool)}, drawn {sum(r['ats'] == ats for r in rows)}, "
            f"dev {sum(r['ats'] == ats and r['split'] == 'dev' for r in rows)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
