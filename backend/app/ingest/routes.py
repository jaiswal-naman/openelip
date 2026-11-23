"""Read API over the job index: /v1/stats, /v1/jobs and /v1/jobs/{job_id} (API.md 2.4).

The models are the contract: the web types are generated from them. Posting text is third-party text, so the fields
that carry it are marked x-untrusted-text in the schema (API.md 1.10), and the web app renders them as plain text.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict
from sqlalchemy import text

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.sessions import current_user

from .search import MODES, STATED_REMOTE, InvalidCursor, get_job, search

router = APIRouter()

MAX_JOB_ID = 2**63 - 1  # job ids are bigints: a larger number cannot be one

UNTRUSTED_TEXT: JsonDict = {"x-untrusted-text": True}


class JobSource(BaseModel):
    ats: Literal["greenhouse", "lever", "ashby", "direct"]
    board: str  # the board's token at that ATS


class Pay(BaseModel):
    min: int | None
    max: int | None
    currency: str  # ISO 4217; annual amounts in whole currency units (API.md 1.4)


class Job(BaseModel):
    id: int
    title: str
    company_name: str
    location_raw: str | None
    country: str | None  # ISO 3166-1 alpha-2; null when the posting's place is not recognised
    city: str | None
    work_arrangements: list[Literal["onsite", "hybrid", "remote"]]  # empty: the posting does not say
    is_remote: bool | None  # for existing clients: true when work_arrangements has remote, null when it is empty
    employment_type: Literal["full_time", "part_time", "contract", "internship", "temporary", "unknown"]
    pay: Pay | None
    apply_url: str
    source: JobSource
    status: Literal["open", "closed"]
    posted_at: datetime | None
    last_verified_at: datetime  # the last fetch that listed this job (IDX-05)
    content_version: int


class Span(BaseModel):
    start: int  # Unicode code points into the description, end exclusive
    end: int


class Requirement(BaseModel):
    id: int
    ordinal: int
    text: str = Field(json_schema_extra=UNTRUSTED_TEXT)  # a quote from the posting
    importance: Literal["required", "preferred"]
    kind: Literal["skill", "experience", "education", "certification", "authorization", "location", "other"]
    span: Span
    min_years: int | None


class JobDetail(Job):
    description: str = Field(json_schema_extra=UNTRUSTED_TEXT)
    closed_at: datetime | None
    requirements_version: int | None
    requirements: list[Requirement]  # the current content version's, or empty while none are extracted


@router.get("/v1/stats")
def stats() -> dict[str, Any]:
    with db.transaction() as conn:
        row = conn.execute(
            text(
                f"""SELECT count(*) FILTER (WHERE status='open'),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL AND country='IN'),
                      count(*) FILTER (WHERE status='open' AND canonical_job_id IS NULL AND {STATED_REMOTE}),
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


@router.get("/v1/jobs", response_model=Envelope[list[Job]])
def search_jobs(
    request: Request,
    q: str = Query("", max_length=200),
    country: str | None = Query(None, pattern="^[A-Za-z]{2}$"),  # an ISO 3166-1 alpha-2 code
    city: str | None = Query(None, max_length=100),
    remote: bool | None = None,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    mode: str = Query("hybrid", json_schema_extra={"enum": list(MODES)}),
    apply_constraints: bool = Query(
        False, description="Signed in only: leave out jobs that break your hard constraints."
    ),
) -> dict[str, Any]:
    """Search open, de-duplicated, listed jobs by keywords and by meaning. Results carry no fit claims.

    meta.mode says what ran: hybrid falls back to keyword when no job has a vector yet or the model is unavailable.
    With apply_constraints the caller's own hard constraints are applied (CHT-02) and meta.constraints_applied is true;
    it needs a session (401 otherwise) and the person is always the session's, never a parameter.
    """
    if mode not in MODES:
        raise ApiError(400, "invalid_request", "mode must be hybrid or keyword.")
    person = current_user(request).user_id if apply_constraints else None
    try:
        with db.transaction(person) as conn:
            page = search(
                conn,
                q=q,
                country=country,
                city=city,
                remote=remote,
                limit=limit,
                cursor=cursor,
                mode=mode,
                constrained_for=person,
            )
    except InvalidCursor:
        raise ApiError(400, "invalid_request", "That cursor does not belong to this search. Start the search again.")
    applied = {"constraints_applied": True} if person else {}  # absent otherwise: the answer is as it always was
    return envelope(
        page.rows, count=len(page.rows), limit=limit, mode=page.mode, next_cursor=page.next_cursor, **applied
    )


@router.get("/v1/jobs/{job_id}", response_model=Envelope[JobDetail])
def read_job(job_id: int = Path(ge=1, le=MAX_JOB_ID)) -> dict[str, Any]:
    """One listed job, open or closed, with its plain-text description and its requirements with character spans."""
    with db.transaction() as conn:
        job = get_job(conn, job_id)
    if job is None:
        raise ApiError(404, "not_found", "No such resource.")
    return envelope(job)
