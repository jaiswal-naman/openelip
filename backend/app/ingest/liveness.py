"""The apply-link re-check (IDX-07): is the page a candidate would apply on still there?

verify_live(conn, job_id) asks the job's own apply link through platform.http.fetch_public. A 404 or 410 closes the job
and a 2xx marks it alive and verified. Anything else (another status, a refused address, a timeout, a dropped
connection) closes nothing, because a bad afternoon at a careers site must not empty the index. A duplicate is checked
through its canonical job, the one a person is shown, and the outcome is written to that job.

No row lock is held while the request is on the wire: the job is read with a plain SELECT, the page is fetched, and then
one UPDATE writes the outcome. Each UPDATE only touches a job that is still open, so a close that happened meanwhile
(the board dropped the posting, another check) stays as it was.
"""

import time

from sqlalchemy import Connection, text

from app.platform import log
from app.platform.http import FetchFailed, UnsafeUrl, fetch_public

DEAD_LINK = (404, 410)

TARGET = text(
    """SELECT t.id, t.status::text AS status, t.apply_url
         FROM jobs j JOIN jobs t ON t.id = COALESCE(j.canonical_job_id, j.id)
        WHERE j.id = :id"""
)
# A job the check closed is the pair status 'closed' and apply_check_ok false: the refresh of an unchanged posting
# (fetch.REFRESH_JOB) reads the second to leave it closed, and only changed text brings it back.
CLOSE = text(
    """UPDATE jobs SET status = 'closed', closed_at = now(), apply_check_ok = false, apply_checked_at = now()
        WHERE id = :id AND status = 'open'"""
)
ALIVE = text(
    """UPDATE jobs SET apply_check_ok = true, apply_checked_at = now(), last_verified_at = now()
        WHERE id = :id AND status = 'open'"""
)
UNSURE = text("UPDATE jobs SET apply_check_ok = NULL, apply_checked_at = now() WHERE id = :id AND status = 'open'")


def _ask(conn: Connection, job_id: int, apply_url: str) -> bool | None:
    try:
        status: int | None = fetch_public(apply_url).status
    except (UnsafeUrl, FetchFailed):
        status = None
    if status in DEAD_LINK:
        conn.execute(CLOSE, {"id": job_id})
        return False
    # ponytail: a 2xx does not prove the role is open. Some career sites answer 200 with a "position filled" page, or
    # send a closed role to their listing. The board feed on the next 6-hourly fetch is the stronger signal; checking
    # the page's content is the upgrade path.
    if status is not None and 200 <= status < 300:
        # A close that landed while the request was out wins: the update finds nothing open to touch.
        return conn.execute(ALIVE, {"id": job_id}).rowcount == 1
    conn.execute(UNSURE, {"id": job_id})
    return None


def verify_live(conn: Connection, job_id: int) -> bool | None:
    """True if the apply link answers, False if the job is closed (by this check or already, or it does not exist),
    None if the check could not tell."""
    started = time.monotonic()
    target = conn.execute(TARGET, {"id": job_id}).one_or_none()
    result: bool | None
    if target is None or target.status != "open":
        result, outcome = False, "not_open"  # no request is made for a job that is not open
    else:
        result = _ask(conn, target.id, target.apply_url)
        outcome = "unknown" if result is None else ("alive" if result else "closed")
    log.event(
        "ingest.verify_live", job_id=job_id, outcome=outcome, duration_ms=round((time.monotonic() - started) * 1000)
    )
    return result
