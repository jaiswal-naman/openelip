"""Read API over the job index: /v1/stats and /v1/jobs (API.md 2.4)."""

from fastapi import APIRouter, Query
from sqlalchemy import text

from app.platform import db
from app.platform.api import envelope

router = APIRouter()


@router.get("/v1/stats")
def stats():
    with db.transaction() as conn:
        row = conn.execute(
            text(
                """SELECT count(*) FILTER (WHERE status='open'),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL AND country='IN'),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL AND is_remote),
                      count(*) FILTER (WHERE status='closed'),
                      count(DISTINCT source_id) FILTER (WHERE status='open'),
                      max(last_verified_at)
               FROM jobs"""
            )
        ).one()
    keys = [
        "open_postings",
        "open_unique_jobs",
        "open_unique_jobs_india",
        "open_unique_jobs_remote",
        "closed_postings",
        "employers_with_open_jobs",
        "last_verified_at",
    ]
    return envelope(dict(zip(keys, row)))


@router.get("/v1/jobs")
def search_jobs(
    q: str = Query("", max_length=200),
    country: str | None = Query(None, min_length=2, max_length=2),
    remote: bool | None = None,
    limit: int = Query(20, ge=1, le=100),
):
    """Keyword search over open, de-duplicated jobs. Results carry no fit claims."""
    where = ["status = 'open'", "canonical_job_id IS NULL"]
    params: dict = {"limit": limit}
    if q.strip():
        where.append("search @@ websearch_to_tsquery('english', :q)")
        params["q"] = q
    if country:
        where.append("country = :country")
        params["country"] = country.upper()
    if remote is not None:
        where.append("is_remote IS NOT DISTINCT FROM :remote")
        params["remote"] = remote
    order = "ts_rank_cd(search, websearch_to_tsquery('english', :q)) DESC, " if q.strip() else ""
    sql = f"""SELECT id, title, company_name, location_raw, country, city, is_remote, apply_url, posted_at,
                     last_verified_at, content_version
              FROM jobs WHERE {" AND ".join(where)}
              ORDER BY {order}posted_at DESC NULLS LAST LIMIT :limit"""
    with db.transaction() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings()]
    return envelope(rows, count=len(rows), limit=limit)
