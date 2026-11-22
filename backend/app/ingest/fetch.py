"""Store one source's postings, close jobs that left their feed, fold duplicates.

These are plain functions over a SQLAlchemy connection. The network fetch and the fetch_runs bookkeeping live in
jobs.py, where each source is one queue job (python -m app.cli ingest run).
"""

from typing import Any

from sqlalchemy import Connection, text

from .normalize import content_hash, dedupe_key, employment_type_from, parse_location, work_arrangements_from

MIN_DESCRIPTION_CHARS = 40

# The enum columns are bound through explicit casts, so the driver's idea of a parameter's type never matters.
INSERT_JOB = text(
    """INSERT INTO jobs (source_id, external_id, title, company_name, location_raw, country, city, work_arrangements,
                        employment_type, employment_type_raw, department, description, apply_url, posted_at,
                        content_hash, dedupe_key)
       VALUES (:source_id, :external_id, :title, :company_name, :location_raw, :country, :city,
               CAST(:work_arrangements AS work_arrangement[]), CAST(:employment_type AS employment_type),
               :employment_type_raw, :department, :description, :apply_url, :posted_at, :content_hash, :dedupe_key)"""
)
# New text is a new content version: everything computed from the old text (the embedding, the apply-link check)
# is cleared with it, and the job counts as materially changed (the nightly run reads materially_changed_at).
UPDATE_JOB = text(
    """UPDATE jobs SET title = :title, company_name = :company_name, location_raw = :location_raw, country = :country,
                       city = :city, work_arrangements = CAST(:work_arrangements AS work_arrangement[]),
                       employment_type = CAST(:employment_type AS employment_type),
                       employment_type_raw = :employment_type_raw, department = :department,
                       description = :description, apply_url = :apply_url, posted_at = :posted_at,
                       content_hash = :content_hash, dedupe_key = :dedupe_key,
                       content_version = content_version + 1, materially_changed_at = now(),
                       embedding = NULL, embedding_model = NULL, apply_checked_at = NULL, apply_check_ok = NULL,
                       status = 'open', closed_at = NULL, last_verified_at = now()
        WHERE id = :id"""
)
# Same text, seen again. The place is read afresh, so a better parser heals old rows on the next fetch (dedupe_key
# is built from country and city, so it follows). A job the apply-link check closed (apply_check_ok = false) is not
# touched: the feed still lists it, but its apply link is dead, and only new text brings it back.
REFRESH_JOB = text(
    """UPDATE jobs SET country = :country, city = :city, work_arrangements = CAST(:work_arrangements AS work_arrangement[]),
                       dedupe_key = :dedupe_key, status = 'open', closed_at = NULL, last_verified_at = now()
        WHERE id = :id AND apply_check_ok IS NOT false"""
)


def may_close(seen: int, open_before: int) -> bool:
    """An empty feed where we had many open jobs is more likely a glitch than a mass closure."""
    return not (seen == 0 and open_before > 5)


def store(conn: Connection, source_id: int, company: str, postings: list[dict[str, Any]]) -> dict[str, Any]:
    existing = {
        row.external_id: (row.id, row.content_hash)
        for row in conn.execute(
            text("SELECT external_id, id, content_hash FROM jobs WHERE source_id = :source_id"),
            {"source_id": source_id},
        )
    }
    open_before = conn.execute(
        text("SELECT count(*) FROM jobs WHERE source_id = :source_id AND status = 'open'"), {"source_id": source_id}
    ).scalar_one()
    counts: dict[str, Any] = {"seen": 0, "new": 0, "changed": 0, "closed": 0}
    seen_ids = []
    for p in postings:
        if len(p["description"]) < MIN_DESCRIPTION_CHARS:
            continue  # a posting with no body cannot be matched or cited
        counts["seen"] += 1
        seen_ids.append(p["external_id"])
        country, city, is_remote = parse_location(p["location_raw"], p["country_hint"], p["remote_hint"])
        chash = content_hash(p["title"], p["description"], p["location_raw"])
        fields = {
            "title": p["title"],
            "company_name": company,
            "location_raw": p["location_raw"],
            "country": country,
            "city": city,
            "work_arrangements": work_arrangements_from(is_remote),
            "employment_type": employment_type_from(p["employment_type"]),
            "employment_type_raw": p["employment_type"],
            "department": p["department"],
            "description": p["description"],
            "apply_url": p["apply_url"],
            "posted_at": p["posted_at"],
            "content_hash": chash,
            "dedupe_key": dedupe_key(company, p["title"], country, city),
        }
        if p["external_id"] not in existing:
            conn.execute(INSERT_JOB, {"source_id": source_id, "external_id": p["external_id"], **fields})
            counts["new"] += 1
        elif existing[p["external_id"]][1] != chash:
            conn.execute(UPDATE_JOB, {"id": existing[p["external_id"]][0], **fields})
            counts["changed"] += 1
        else:
            conn.execute(REFRESH_JOB, {"id": existing[p["external_id"]][0], **fields})
    if may_close(counts["seen"], open_before):
        counts["closed"] = conn.execute(
            text(
                """UPDATE jobs SET status = 'closed', closed_at = now()
                   WHERE source_id = :source_id AND status = 'open'
                     AND NOT (external_id = ANY(CAST(:seen_ids AS text[])))"""
            ),
            {"source_id": source_id, "seen_ids": seen_ids},
        ).rowcount
        counts["status"] = "ok"
    else:
        counts["status"] = "suspicious_empty"
    return counts


def mark_duplicates(conn: Connection) -> int:
    """Within one company, postings with the same title and place collapse onto the oldest one.

    ponytail: same-company duplicates only. Cross-company reposts (agencies) need text similarity; add when a second
    kind of source is registered.
    """
    conn.execute(text("UPDATE jobs SET canonical_job_id = NULL WHERE canonical_job_id IS NOT NULL"))
    return conn.execute(
        text(
            """UPDATE jobs j SET canonical_job_id = c.keep
               FROM (SELECT dedupe_key, min(id) AS keep FROM jobs WHERE status = 'open'
                     GROUP BY dedupe_key HAVING count(*) > 1) c
               WHERE j.dedupe_key = c.dedupe_key AND j.status = 'open' AND j.id <> c.keep"""
        )
    ).rowcount
