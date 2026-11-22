"""Search over the job index (IDX-04, IDX-05): keywords and meaning at once, and one job by id.

search() is one SQL statement shaped like DATA_MODEL 6.1, without the constraint and match-decision parts (Phase 4):

    eligible   the open, canonical, listed jobs that pass the filters. It is materialized, so the vector list is an
               exact scan over the filtered rows and an approximate index can never drop a row the filters kept.
    dense      the 200 nearest by vector distance   (hybrid mode only, and only when the query could be embedded)
    sparse     the 200 best by ts_rank_cd for the keywords
    fused      each job's score is the sum of 1 / (60 + rank) over the lists it is in: reciprocal rank fusion

Keyword mode is the sparse list alone. With no query text the list is every eligible job, newest first. Pages are
keyset pages: the cursor holds the last row's sort key and id, so the next page is "strictly after" it.
"""

import base64
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Connection, Row, TextClause, text

from app.platform import log
from app.platform.llm.embed import EmbeddingUnavailable, embed_query

from .embedding import vector_literal

RRF_K = 60  # the conventional constant of reciprocal rank fusion
LIST_DEPTH = 200  # rows each list keeps before fusion (DATA_MODEL 6.1), so a search reaches at most 400 jobs
MODES = ("hybrid", "keyword")

# API.md 2.4: is_remote stays for existing clients and is read off work_arrangements. It is true when the array holds
# remote, null when the array is empty (the posting does not say) and false otherwise. The remote filter and the
# remote count of /v1/stats apply the same rule.
IS_REMOTE = """(CASE WHEN 'remote' = ANY(work_arrangements) THEN true
                    WHEN cardinality(work_arrangements) = 0 THEN NULL
                    ELSE false END)"""
STATED_REMOTE = "'remote' = ANY(work_arrangements)"
STATED_NOT_REMOTE = "(cardinality(work_arrangements) > 0 AND NOT ('remote' = ANY(work_arrangements)))"

# The fields of a job in a list and in the detail (API.md 2.4, the job object in 2.5).
JOB_COLUMNS = f"""j.id, j.title, j.company_name, j.location_raw, j.country, j.city,
       j.work_arrangements::text[] AS work_arrangements, {IS_REMOTE} AS is_remote,
       j.employment_type::text AS employment_type, j.salary_min, j.salary_max, j.salary_currency, j.apply_url,
       s.ats::text AS ats, s.token AS board, j.status::text AS status, j.posted_at, j.last_verified_at,
       j.content_version"""

DENSE = f"""dense AS (
    SELECT id, row_number() OVER (ORDER BY distance, id) AS r
      FROM (SELECT e.id, e.embedding <=> CAST(:vector AS vector) AS distance
              FROM eligible e WHERE e.embedding IS NOT NULL
             ORDER BY distance, e.id LIMIT {LIST_DEPTH}) nearest
)"""
SPARSE = f"""sparse AS (
    SELECT id, row_number() OVER (ORDER BY relevance DESC, id) AS r
      FROM (SELECT e.id, ts_rank_cd(e.search, tsq) AS relevance
              FROM eligible e, websearch_to_tsquery('english', :query) AS tsq
             WHERE e.search @@ tsq
             ORDER BY relevance DESC, e.id LIMIT {LIST_DEPTH}) best
)"""
# Newest first, and a job with no posted date after every dated one.
LISTING_KEY = "coalesce(j.posted_at, '-infinity'::timestamptz)"

JOB = text(
    f"""SELECT {JOB_COLUMNS}, j.description, j.closed_at, j.requirements_version
          FROM jobs j JOIN job_sources s ON s.id = j.source_id
         WHERE j.id = :id AND j.is_listed"""
)
REQUIREMENTS = text(
    """SELECT id, ordinal, text, importance::text AS importance, kind::text AS kind,
              lower(span) AS span_start, upper(span) AS span_end, min_years
         FROM job_requirements WHERE job_id = :id AND content_version = :version ORDER BY ordinal"""
)


class InvalidCursor(Exception):
    """The cursor does not decode, or belongs to another search than the one asked for."""


@dataclass(frozen=True)
class SearchPage:
    rows: list[dict[str, Any]]
    next_cursor: str | None
    mode: str  # the mode that was used: "hybrid" falls back to "keyword" when meaning cannot be searched


def _clean(value: str | None) -> str:
    """Text a person typed: a NUL (PostgreSQL text cannot hold one) counts as a space, and white space is collapsed."""
    return " ".join((value or "").replace("\x00", " ").split())


def _utc(moment: datetime | None) -> datetime | None:
    """API.md 1.4: timestamps are UTC, whatever time zone the database session happens to use."""
    return None if moment is None else moment.astimezone(timezone.utc)


def _job(row: Row[Any]) -> dict[str, Any]:
    pay = None
    if row.salary_min is not None or row.salary_max is not None:
        pay = {"min": row.salary_min, "max": row.salary_max, "currency": row.salary_currency}
    return {
        "id": row.id,
        "title": row.title,
        "company_name": row.company_name,
        "location_raw": row.location_raw,
        "country": row.country,
        "city": row.city,
        "work_arrangements": row.work_arrangements,
        "is_remote": row.is_remote,
        "employment_type": row.employment_type,
        "pay": pay,
        "apply_url": row.apply_url,
        "source": {"ats": row.ats, "board": row.board},
        "status": row.status,
        "posted_at": _utc(row.posted_at),
        "last_verified_at": _utc(row.last_verified_at),
        "content_version": row.content_version,
    }


def _eligible(country: str | None, city: str | None, remote: bool | None) -> tuple[str, dict[str, Any]]:
    """The WHERE clause of the jobs a search may return, and its bound values. Filters narrow before any ranking."""
    where = ["j.status = 'open'", "j.canonical_job_id IS NULL", "j.is_listed"]
    values: dict[str, Any] = {}
    if country:
        where.append("j.country = :country")
        values["country"] = country
    if city:
        where.append("lower(j.city) = lower(:city)")
        values["city"] = city
    if remote is not None:
        where.append(STATED_REMOTE if remote else STATED_NOT_REMOTE)
    return " AND ".join(where), values


def _query_vector(conn: Connection, where: str, values: dict[str, Any], query: str) -> list[float] | None:
    """The query's vector, or None when meaning cannot be searched: nothing eligible is embedded yet (the model is not
    even loaded then), or the model is not available here."""
    embedded = conn.execute(
        text(f"SELECT EXISTS (SELECT 1 FROM jobs j WHERE {where} AND j.embedding IS NOT NULL)"), values
    ).scalar_one()
    if not embedded:
        return None
    try:
        return embed_query(query)
    except EmbeddingUnavailable:
        return None


def _ranked(where: str, *, dense: bool, after: bool) -> TextClause:
    lists = ["dense", "sparse"] if dense else ["sparse"]
    union = " UNION ALL ".join(f"SELECT id, r FROM {name}" for name in lists)
    columns = "j.id, j.search, j.embedding" if dense else "j.id, j.search"
    ctes = [f"eligible AS MATERIALIZED (SELECT {columns} FROM jobs j WHERE {where})"]
    if dense:
        ctes.append(DENSE)
    ctes.append(SPARSE)
    ctes.append(f"fused AS (SELECT id, sum(1.0::float8 / ({RRF_K} + r)) AS score FROM ({union}) ranked GROUP BY id)")
    keyset = "WHERE fused.score < :after_key OR (fused.score = :after_key AND fused.id > :after_id)" if after else ""
    return text(
        f"""WITH {", ".join(ctes)}
            SELECT {JOB_COLUMNS}, fused.score
              FROM fused JOIN jobs j ON j.id = fused.id JOIN job_sources s ON s.id = j.source_id
              {keyset}
             ORDER BY fused.score DESC, fused.id LIMIT :fetch"""
    )


def _listing(where: str, *, after: bool) -> TextClause:
    keyset = f"AND ({LISTING_KEY}, j.id) < (CAST(:after_key AS timestamptz), :after_id)" if after else ""
    return text(
        f"""SELECT {JOB_COLUMNS}
              FROM jobs j JOIN job_sources s ON s.id = j.source_id
             WHERE {where} {keyset}
             ORDER BY {LISTING_KEY} DESC, j.id DESC LIMIT :fetch"""
    )


def _fingerprint(query: str, country: str | None, city: str | None, remote: bool | None) -> str:
    """What identifies a search: a cursor is good only for the search that made it."""
    shape = json.dumps([query, country, city and city.lower(), remote], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(shape.encode()).hexdigest()[:16]


def _encode_cursor(mode: str, fingerprint: str, key: float | str, job_id: int) -> str:
    body = json.dumps({"m": mode, "f": fingerprint, "k": key, "i": job_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, mode: str, fingerprint: str, *, listing: bool) -> tuple[float | str, int]:
    """The (sort key, id) the next page starts after. Everything in the cursor is checked, so only values of the right
    type ever reach the database."""
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        key, job_id = body["k"], body["i"]
        if body["m"] != mode or body["f"] != fingerprint or isinstance(job_id, bool) or not isinstance(job_id, int):
            raise InvalidCursor
        if listing:
            if not isinstance(key, str):
                raise InvalidCursor
            if key != "-infinity":
                datetime.fromisoformat(key)  # raises ValueError when it is not a date
        elif isinstance(key, bool) or not isinstance(key, int | float) or not math.isfinite(key):
            raise InvalidCursor
    except (ValueError, KeyError, TypeError) as error:  # ValueError covers bad base64, bad JSON and bad dates
        raise InvalidCursor from error
    return key, job_id


def search(
    conn: Connection,
    *,
    q: str,
    country: str | None,
    city: str | None,
    remote: bool | None,
    limit: int,
    cursor: str | None,
    mode: str,
) -> SearchPage:
    """One page of open, canonical, listed jobs. The page's `mode` says what ran: hybrid becomes keyword when no
    eligible job has a vector yet or the model is unavailable. With no query text, jobs are listed newest first and the
    mode is the one asked for. Raises InvalidCursor for a cursor that is not this search's."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    started = time.monotonic()
    query = _clean(q)
    country = country.upper() if country else None
    city = _clean(city) or None
    where, values = _eligible(country, city, remote)
    vector = _query_vector(conn, where, values, query) if query and mode == "hybrid" else None
    if not query:
        used = mode  # nothing to rank: the list is the same in both modes
    else:
        used = "hybrid" if vector is not None else "keyword"
    fingerprint = _fingerprint(query, country, city, remote)
    after = _decode_cursor(cursor, used, fingerprint, listing=not query) if cursor else None

    values = {**values, "fetch": limit + 1}
    if after is not None:
        values["after_key"], values["after_id"] = after
    if not query:
        statement = _listing(where, after=after is not None)
    else:
        values["query"] = query
        if vector is not None:
            values["vector"] = vector_literal(vector)
        statement = _ranked(where, dense=vector is not None, after=after is not None)
    rows = conn.execute(statement, values).all()

    shown, more = rows[:limit], len(rows) > limit
    next_cursor = None
    if more:
        last = shown[-1]
        key = last.score if query else (last.posted_at.isoformat() if last.posted_at else "-infinity")
        next_cursor = _encode_cursor(used, fingerprint, key, last.id)
    log.event(
        "jobs.search", method=used, result_count=len(shown), duration_ms=round((time.monotonic() - started) * 1000)
    )
    return SearchPage([_job(row) for row in shown], next_cursor, used)


def get_job(conn: Connection, job_id: int) -> dict[str, Any] | None:
    """One listed job, open or closed, with its description and the requirements of its current content version.
    The requirements are shown only when the job says their extraction is complete for that version."""
    row = conn.execute(JOB, {"id": job_id}).one_or_none()
    if row is None:
        return None
    requirements: list[dict[str, Any]] = []
    if row.requirements_version == row.content_version:
        requirements = [
            {
                "id": found.id,
                "ordinal": found.ordinal,
                "text": found.text,
                "importance": found.importance,
                "kind": found.kind,
                "span": {"start": found.span_start, "end": found.span_end},
                "min_years": found.min_years,
            }
            for found in conn.execute(REQUIREMENTS, {"id": job_id, "version": row.content_version})
        ]
    return {
        **_job(row),
        "description": row.description,
        "closed_at": _utc(row.closed_at),
        "requirements_version": row.requirements_version,
        "requirements": requirements,
    }
