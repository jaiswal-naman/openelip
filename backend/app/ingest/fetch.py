"""Fetch every active source, upsert jobs, close jobs that left their feed.

Run:  python -m app.ingest.fetch
"""
import asyncio
import sys

import httpx

from app.platform import db

from .adapters import fetch_postings
from .normalize import content_hash, dedupe_key, parse_location

CONCURRENCY = 6
MIN_DESCRIPTION_CHARS = 40


def may_close(seen: int, open_before: int) -> bool:
    """An empty feed where we had many open jobs is more likely a glitch than a mass closure."""
    return not (seen == 0 and open_before > 5)


def store(conn, source_id: int, company: str, postings: list[dict]) -> dict:
    existing = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT external_id, id, content_hash FROM jobs WHERE source_id = %s", (source_id,))
    }
    open_before = conn.execute(
        "SELECT count(*) FROM jobs WHERE source_id = %s AND status = 'open'", (source_id,)
    ).fetchone()[0]
    counts = {"seen": 0, "new": 0, "changed": 0, "closed": 0}
    seen_ids = []
    for p in postings:
        if len(p["description"]) < MIN_DESCRIPTION_CHARS:
            continue  # a posting with no body cannot be matched or cited
        counts["seen"] += 1
        seen_ids.append(p["external_id"])
        country, city, is_remote = parse_location(p["location_raw"], p["country_hint"], p["remote_hint"])
        chash = content_hash(p["title"], p["description"], p["location_raw"])
        fields = (
            p["title"], company, p["location_raw"], country, city, is_remote, p["employment_type"], p["department"],
            p["description"], p["apply_url"], p["posted_at"], chash, dedupe_key(company, p["title"], country, city),
        )
        if p["external_id"] not in existing:
            conn.execute(
                """INSERT INTO jobs (source_id, external_id, title, company_name, location_raw, country, city, is_remote,
                                     employment_type, department, description, apply_url, posted_at, content_hash, dedupe_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (source_id, p["external_id"], *fields),
            )
            counts["new"] += 1
        elif existing[p["external_id"]][1] != chash:
            conn.execute(
                """UPDATE jobs SET title=%s, company_name=%s, location_raw=%s, country=%s, city=%s, is_remote=%s,
                                   employment_type=%s, department=%s, description=%s, apply_url=%s, posted_at=%s,
                                   content_hash=%s, dedupe_key=%s, content_version = content_version + 1,
                                   status='open', closed_at=NULL, last_verified_at=now()
                   WHERE id=%s""",
                (*fields, existing[p["external_id"]][0]),
            )
            counts["changed"] += 1
        else:
            conn.execute(
                "UPDATE jobs SET status='open', closed_at=NULL, last_verified_at=now() WHERE id=%s",
                (existing[p["external_id"]][0],),
            )
    if may_close(counts["seen"], open_before):
        counts["closed"] = conn.execute(
            """UPDATE jobs SET status='closed', closed_at=now()
               WHERE source_id=%s AND status='open' AND NOT (external_id = ANY(%s))""",
            (source_id, seen_ids),
        ).rowcount
        counts["status"] = "ok"
    else:
        counts["status"] = "suspicious_empty"
    return counts


def mark_duplicates(conn) -> int:
    """Within one company, postings with the same title and place collapse onto the oldest one.

    ponytail: same-company duplicates only. Cross-company reposts (agencies) need text similarity; add when a second
    kind of source is registered.
    """
    conn.execute("UPDATE jobs SET canonical_job_id = NULL WHERE canonical_job_id IS NOT NULL")
    return conn.execute(
        """UPDATE jobs j SET canonical_job_id = c.keep
           FROM (SELECT dedupe_key, min(id) AS keep FROM jobs WHERE status='open'
                 GROUP BY dedupe_key HAVING count(*) > 1) c
           WHERE j.dedupe_key = c.dedupe_key AND j.status='open' AND j.id <> c.keep"""
    ).rowcount


async def run() -> None:
    with db.connect() as conn:
        sources = conn.execute("SELECT id, ats, token, company_name FROM job_sources WHERE is_active ORDER BY id").fetchall()
    if not sources:
        print("No sources registered. Run: python -m app.discover")
        return
    sem = asyncio.Semaphore(CONCURRENCY)
    totals = {"ok": 0, "failed": 0, "seen": 0, "new": 0, "changed": 0, "closed": 0}

    async with httpx.AsyncClient(follow_redirects=False) as client:
        async def one(source):
            sid, ats, token, company = source
            async with sem:
                status, http_status, postings, error = await fetch_postings(client, ats, token)
            with db.connect() as conn:
                counts = {"seen": 0, "new": 0, "changed": 0, "closed": 0}
                if status == "ok":
                    counts = store(conn, sid, company, postings)
                    status = counts.pop("status")
                conn.execute(
                    """INSERT INTO fetch_runs (source_id, finished_at, status, http_status, jobs_seen, jobs_new,
                                               jobs_changed, jobs_closed, error)
                       VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s)""",
                    (sid, status, http_status, counts["seen"], counts["new"], counts["changed"], counts["closed"], error),
                )
                conn.execute("UPDATE job_sources SET last_fetched_at=now(), last_status=%s WHERE id=%s", (status, sid))
            totals["ok" if status == "ok" else "failed"] += 1
            for k in ("seen", "new", "changed", "closed"):
                totals[k] += counts[k]
            if status != "ok":
                print(f"  {ats}/{token}: {status} {http_status or ''} {error or ''}".rstrip())

        await asyncio.gather(*(one(s) for s in sources))

    with db.connect() as conn:
        dupes = mark_duplicates(conn)
    print(f"sources ok={totals['ok']} failed={totals['failed']} | jobs seen={totals['seen']} new={totals['new']} "
          f"changed={totals['changed']} closed={totals['closed']} | duplicates folded={dupes}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
