"""Freeze a snapshot of public postings for the matching benchmark (plan 04-07; threats T-04-37, T-04-38).

    cd backend
    uv run python ../evals/matching_benchmark/export_snapshot.py --source-url postgresql://...@127.0.0.1:5433/<database>

It reads the job index of one database in a session the server itself holds read-only (`default_transaction_read_only`),
so it cannot write even by mistake. It refuses a source whose Alembic revision is below the Phase 2/3 merge: until the
owner upgrades the live database `app`, that is every export from `app`, and the way round it is to restore a pg_dump of
`app` into a scratch database named `..._test`, upgrade that copy to head, and export from the copy (`--source-note`
says so in snapshot.meta.json).

It picks open, listed, canonical postings with no email address or phone number in them (the rule of the other gold
sets), in a stable order that spreads over sources and countries, and writes the fields the Phase 2 store path needs
(snapshot.jsonl) and where they came from (snapshot.meta.json). Both files are frozen: it will not overwrite them,
because the labels refer to the postings by `ref`.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SNAPSHOT = HERE / "snapshot.jsonl"
META = HERE / "snapshot.meta.json"
DEFAULT_SOURCE = "postgresql://app:app_local_only@127.0.0.1:5433/app"  # the live index; the revision check refuses it
MERGE_REVISION = "p3_0303_skill_fks"  # the single head after Phases 2 and 3 merged (04-01's down_revision)
TARGET_COUNT = 1000
MAX_BYTES = 5_000_000  # the plan's size limit for a committed snapshot: lower the count rather than exceed it

# A posting with an email address or phone number in it is never kept (T-04-38). The same two patterns are in
# evals/requirement_extraction/build_sample.py and evals/location/build_sample.py; the email one here also takes an
# address written name[at]domain or name (at) domain, which the first export let through (21 postings, one role mailbox).
EMAIL = re.compile(r"[\w.+-]+(?:@|\s*[\[(]at[\])]\s*)[\w-]+(\.[\w-]+)+", re.IGNORECASE)
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d|\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}\b")

POOL_SQL = """
SELECT s.ats::text AS ats, s.token, s.company_name AS company, j.external_id, j.title, j.description, j.location_raw,
       j.country, j.work_arrangements::text[] AS work_arrangements, j.employment_type_raw AS employment_type,
       j.department, j.apply_url, j.posted_at, j.materially_changed_at AS changed_at
  FROM jobs j JOIN job_sources s ON s.id = j.source_id
 WHERE j.status = 'open' AND j.is_listed AND j.canonical_job_id IS NULL
"""
RULE = (
    "Postings with an email address or phone number are dropped. The rest are grouped by (source, country) and ordered "
    "by the SHA-256 of ats/token/external_id inside each group; the snapshot is the first N of the list sorted by "
    "(position inside the group, hash), so it takes one posting from every group, then a second from every group "
    "that has one, and so on. N is 1,000 or, if the file would pass 5 MB, the most that fits."
)


def has_contact_data(*texts: str | None) -> bool:
    return any(t and (EMAIL.search(t) or PHONE.search(t)) for t in texts)


def descends_from(revision: str, base: str = MERGE_REVISION) -> bool:
    """Whether `revision` is `base` or a later revision of the migration history (Alembic's own walk of it). A revision
    the history does not know is not a descendant."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config)
    try:
        return base in {r.revision for r in script.walk_revisions(base="base", head=revision)}
    except CommandError:
        return False


def stable_key(ats: str, token: str, external_id: str) -> str:
    return hashlib.sha256(f"{ats}/{token}/{external_id}".encode()).hexdigest()


def ref_of(ats: str, token: str, external_id: str) -> str:
    """The stable reference of a posting, the same one snapshot.jsonl carries: a database id changes on every load, this
    does not."""
    return "j" + stable_key(ats, token, external_id)[:10]


def remote_hint(arrangements: list[str]) -> bool | None:
    """The is_remote the store path reads (ingest.normalize.work_arrangements_from, inverted): ['remote'] was a remote
    posting, ['onsite', 'hybrid'] one that is not, and an empty list a posting that did not say."""
    return True if arrangements == ["remote"] else False if arrangements else None


def to_record(row: dict[str, Any], key: str) -> dict[str, Any]:
    """The arguments of ingest.fetch.store for one posting, plus a stable ref and when the index first saw it. The
    country the index stored goes in as country_hint (the board's hint is not stored), so the store path reads the
    same place again."""
    return {
        "ref": "j" + key[:10],
        "ats": row["ats"],
        "token": row["token"],
        "company": row["company"],
        "external_id": row["external_id"],
        "title": row["title"],
        "description": row["description"],
        "location_raw": row["location_raw"],
        "country_hint": row["country"],
        "remote_hint": remote_hint(row["work_arrangements"]),
        "employment_type": row["employment_type"],
        "department": row["department"],
        "apply_url": row["apply_url"],
        "posted_at": row["posted_at"].isoformat() if row["posted_at"] else None,
        "changed_at": row["changed_at"].isoformat(),
    }


def order(pool: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The stable, spread-out order of RULE, for (hash, posting) pairs: by position inside the (source, country) group,
    then by hash."""
    groups: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, p in pool:
        groups[(p["ats"], p["token"], p["country_hint"] or "")].append((key, p))
    ranked = [
        (rank, key, p)
        for group in groups.values()
        for rank, (key, p) in enumerate(sorted(group, key=lambda pair: pair[0]))
    ]
    return [p for _, _, p in sorted(ranked, key=lambda r: (r[0], r[1]))]


def dump(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def fit(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first TARGET_COUNT postings, or the most of them whose file stays under MAX_BYTES (a prefix of the order, so
    the smaller set is the larger one cut short). Sizes add up: the file is the sum of its lines."""
    chosen, size = [], 0
    for p in ordered[:TARGET_COUNT]:
        line = len(dump([p]))
        if size + line > MAX_BYTES:
            break
        chosen.append(p)
        size += line
    return chosen


def read_pool(source_url: str) -> tuple[list[dict[str, Any]], str]:
    import psycopg
    from psycopg.rows import dict_row

    # The server refuses every write on this connection (T-04-37), whatever the SQL says.
    with psycopg.connect(source_url, options="-c default_transaction_read_only=on", row_factory=dict_row) as conn:
        conn.read_only = True
        assert conn.execute("SHOW default_transaction_read_only").fetchone()["default_transaction_read_only"] == "on"
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        if not descends_from(revision):
            sys.exit(
                f"refusing to export: the source is at Alembic revision {revision}, below {MERGE_REVISION}. Restore a "
                "pg_dump of it into a scratch database ending in _test, upgrade that copy to head and export from it"
            )
        return conn.execute(POOL_SQL).fetchall(), revision


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-url", default=DEFAULT_SOURCE, help="the database to read (default: the live `app`)")
    ap.add_argument("--source-note", default="", help="how the source was made, for example 'restored pg_dump of app'")
    args = ap.parse_args()
    if SNAPSHOT.exists() or META.exists():
        print("snapshot.jsonl and snapshot.meta.json exist and are frozen; refusing to overwrite them", file=sys.stderr)
        return 1
    rows, revision = read_pool(args.source_url)
    kept, contact = [], 0
    for r in rows:
        if has_contact_data(r["title"], r["description"], r["location_raw"], r["apply_url"]):
            contact += 1
        else:
            key = stable_key(r["ats"], r["token"], r["external_id"])
            kept.append((key, to_record(r, key)))
    chosen = fit(order(kept))
    assert len({p["ref"] for p in chosen}) == len(chosen), "two postings share a ref"
    SNAPSHOT.write_bytes(dump(chosen))
    database = args.source_url.rsplit("/", 1)[-1].split("?")[0]
    meta = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"database": database, "alembic_revision": revision, "note": args.source_note},
        "selection": {
            "pool_sql": " ".join(POOL_SQL.split()),
            "rule": RULE,
            "open_listed_canonical": len(rows),
            "dropped_for_contact_data": contact,
            "pool": len(kept),
            "target_count": TARGET_COUNT,
            "count": len(chosen),
            "size_bytes": SNAPSHOT.stat().st_size,
            "size_limit_bytes": MAX_BYTES,
        },
        "sources": len({(p["ats"], p["token"]) for p in chosen}),
        "by_ats": dict(sorted(Counter(p["ats"] for p in chosen).items())),
        "by_country": dict(Counter(p["country_hint"] or "not stated" for p in chosen).most_common()),
    }
    META.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(meta["selection"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
