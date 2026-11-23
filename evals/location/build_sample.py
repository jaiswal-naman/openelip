"""Draw the location gold sample from the public job boards the index reads, or validate the committed file.

    uv run --project backend python evals/location/build_sample.py           # build: needs the internet
    uv run --project backend python evals/location/build_sample.py --check   # offline: validate sample.jsonl

Building reads the active board list from the database named by DATABASE_URL (read-only session, SELECT only),
fetches each board through the index's own adapter, and writes an unlabelled sample.jsonl. It refuses to overwrite an
existing file: the sample is frozen, and a second draw would drop the labels. The labels are added by hand, following
GUIDELINE.md.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path

SAMPLE = Path(__file__).with_name("sample.jsonl")
SEED = 20261122
SIZE = 400
DEV = 100
ATS_NAMES = {"greenhouse", "lever", "ashby"}
FIELDS = (
    "id split ats board external_id location_raw country_hint remote_hint label_country label_city labeller".split()
)

# A posting with an email address or phone number in it is left out of the sample (never kept, never labelled).
# The same two patterns are in evals/requirement_extraction/build_sample.py.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}\b")


def has_contact_data(*texts: str | None) -> bool:
    return any(t and (EMAIL.search(t) or PHONE.search(t)) for t in texts)


def active_sources(database_url: str) -> list[tuple[str, str]]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        conn.read_only = True  # the server refuses any write on this connection
        return conn.execute("SELECT ats, token FROM job_sources WHERE is_active ORDER BY ats, token").fetchall()


async def collect(sources: list[tuple[str, str]], concurrency: int):
    """Fetch every board; keep only the location inputs. Returns (pool, excluded_for_contact_data, failed_boards)."""
    import httpx

    from app.ingest.adapters import fetch_postings

    gate = asyncio.Semaphore(concurrency)
    pool: list[dict] = []
    failed: list[str] = []
    excluded = 0

    async def one(client: httpx.AsyncClient, ats: str, board: str) -> None:
        nonlocal excluded
        async with gate:
            status, http_status, postings, _ = await fetch_postings(client, ats, board)
        if status != "ok":
            failed.append(f"{ats}/{board}: {status} {http_status or ''}".strip())
            return
        for p in postings:
            if has_contact_data(p["description"], p["title"], p["location_raw"]):
                excluded += 1
                continue
            pool.append(
                {
                    "ats": ats,
                    "board": board,
                    "external_id": p["external_id"],
                    "location_raw": p["location_raw"],
                    "country_hint": p["country_hint"],
                    "remote_hint": p["remote_hint"],
                }
            )

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(client, a, t) for a, t in sources))
    return pool, excluded, failed


def quotas(counts: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` across ATS names in proportion to `counts` (largest remainder, ties by name)."""
    pool = sum(counts.values())
    exact = {a: total * n / pool for a, n in counts.items()}
    out = {a: int(x) for a, x in exact.items()}
    for a in sorted(exact, key=lambda a: (out[a] - exact[a], a))[: total - sum(out.values())]:
        out[a] += 1
    return out


def draw(pool: list[dict]) -> list[dict]:
    """Proportional by ATS, from a pool sorted first so network arrival order cannot change the draw."""
    rng = random.Random(SEED)
    by_ats: dict[str, list[dict]] = {}
    for p in sorted(pool, key=lambda p: (p["ats"], p["board"], p["external_id"])):
        by_ats.setdefault(p["ats"], []).append(p)
    quota = quotas({a: len(v) for a, v in by_ats.items()}, SIZE)
    drawn = [p for a in sorted(by_ats) for p in rng.sample(by_ats[a], quota[a])]
    rng.shuffle(drawn)  # so the first DEV rows are a random hundred, not all of one board type
    return [
        {"id": f"loc-{i:03d}", "split": "dev" if i <= DEV else "test", **p}
        | {"label_country": None, "label_city": None, "labeller": None}
        for i, p in enumerate(drawn, 1)
    ]


def build(concurrency: int) -> int:
    if SAMPLE.exists():
        print(f"{SAMPLE.name} exists and is frozen; refusing to overwrite it", file=sys.stderr)
        return 1
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("set DATABASE_URL (read-only use)", file=sys.stderr)
        return 1
    sources = active_sources(database_url)
    pool, excluded, failed = asyncio.run(collect(sources, concurrency))
    rows = draw(pool)
    with SAMPLE.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    boards = {(p["ats"], p["board"]) for p in pool}
    print(f"sources {len(sources)}, failed {len(failed)}: {failed}")
    print(f"excluded for contact data: {excluded}; boards left with postings: {len(boards)}")
    for ats in sorted(ATS_NAMES):
        print(
            f"{ats}: pool {sum(p['ats'] == ats for p in pool)}, drawn {sum(r['ats'] == ats for r in rows)}, "
            f"dev {sum(r['ats'] == ats and r['split'] == 'dev' for r in rows)}"
        )
    return 0


def check() -> int:
    problems: list[str] = []
    rows = [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != SIZE:
        problems.append(f"{len(rows)} rows, expected {SIZE}")
    splits = [r.get("split") for r in rows]
    n_dev, n_test = splits.count("dev"), splits.count("test")
    if (n_dev, n_test) != (DEV, SIZE - DEV):
        problems.append(f"splits are {n_dev} dev and {n_test} test, expected {DEV} and {SIZE - DEV}")
    ids = [r.get("id") for r in rows]
    postings = [(r.get("ats"), r.get("board"), r.get("external_id")) for r in rows]
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    if len(set(postings)) != len(postings):
        problems.append("the same posting drawn twice")
    for r in rows:
        where = r.get("id")
        if list(r) != FIELDS:
            problems.append(f"{where}: fields are {list(r)}")
            continue
        if r["ats"] not in ATS_NAMES:
            problems.append(f"{where}: unknown ats {r['ats']!r}")
        if r["remote_hint"] not in (True, False, None):
            problems.append(f"{where}: remote_hint {r['remote_hint']!r}")
        if not r["labeller"]:
            problems.append(f"{where}: not labelled")
        if r["label_country"] is not None and not re.fullmatch(r"[A-Z]{2}", str(r["label_country"])):
            problems.append(f"{where}: label_country {r['label_country']!r}")
        if r["label_city"] is not None and not (isinstance(r["label_city"], str) and r["label_city"].strip()):
            problems.append(f"{where}: label_city {r['label_city']!r}")
        if has_contact_data(r["location_raw"], r["country_hint"]):  # ids are not scanned: UUIDs look like phone numbers
            problems.append(f"{where}: contact data")
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"ok: {len(rows)} rows, {n_dev} dev, {n_test} test, all labelled")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="validate sample.jsonl instead of building it")
    ap.add_argument("--concurrency", type=int, default=6, help="boards fetched at once (default 6)")
    args = ap.parse_args()
    return check() if args.check else build(args.concurrency)


if __name__ == "__main__":
    sys.exit(main())
