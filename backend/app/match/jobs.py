"""Queue handlers of the match module: when runs happen (ARCHITECTURE 5.3 and 5.4; MAT-02, BRF-03).

    match.zones             every minute: once local time passes 03:00 in a time zone that has people to match, plan
                            that zone's local date (so a day that skips 03:00 still runs)
    match.plan              one zone and local date: each person due tonight becomes a match.nightly job (llm queue)
    match.nightly           one person's nightly run
    match.onboarding_sweep  every minute: anyone with a confirmed record and no run yet gets a first run
    match.run               that first run, on the interactive queue

Every job has a unique key, so a repeat of the same zone, person and date adds nothing (a second worker, a late
retry). Each handler takes the time as an argument where it matters, so the tests set it. The model is only reached by
the runs, one person at a time, never by the planning.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError

from app.platform import log
from app.platform.llm import ProviderUnavailable
from app.platform.queue import NonRetryable, enqueue, handler
from app.platform.scheduler import every

from . import service
from .queries import nightly_key

NIGHTLY_START = time(3, 0)  # assumption (ARCHITECTURE 5.3): local time after which a zone's nightly runs are planned
PACING_LIMIT = timedelta(minutes=20)  # assumption (ARCHITECTURE 5.8): plan nothing while the llm queue is this behind
PLAN_ATTEMPTS = 20  # a plan job that has to wait for pacing fails and is retried, so it needs more attempts than most
# assumption: one run scores up to 40 jobs one after another, which can outlast the queue's default five-minute lease;
# a run handed back to a second worker while the first still works would spend twice (it would stay correct, see
# service.py, but cost double).
RUN_LEASE_SECONDS = 1800
SWEEP_SECONDS = 60

# DATA_MODEL 6.2 for one time zone and local date, without the clauses for approvals and agent actions (Phase 7). Due by
# the person's own calendar, not run yet that day, and with something new: a changed record, an open job that is new or
# changed since the last run, or (ARCHITECTURE 4.3) jobs the daily cap held back last time.
DUE_TONIGHT = text(
    """SELECT u.id
         FROM users u
         LEFT JOIN LATERAL (
              SELECT r.id, r.started_at, r.record_version FROM match_runs r
               WHERE r.user_id = u.id AND r.status = 'completed' AND r.trigger <> 'role'
               ORDER BY r.started_at DESC LIMIT 1
         ) last_run ON true
        WHERE u.status = 'active' AND u.timezone = :zone AND u.record_confirmed_at IS NOT NULL
          AND u.brief_frequency <> 'paused'
          AND (u.brief_frequency = 'daily' OR u.brief_weekday = extract(isodow FROM CAST(:day AS date)))
          AND NOT EXISTS (SELECT 1 FROM match_runs r WHERE r.user_id = u.id AND r.trigger = 'nightly'
                                                       AND r.local_date = :day)
          AND (last_run.id IS NULL
               OR last_run.record_version < u.record_version
               OR EXISTS (SELECT 1 FROM jobs j WHERE j.status = 'open' AND j.canonical_job_id IS NULL AND j.is_listed
                                                 AND j.materially_changed_at > last_run.started_at)
               OR EXISTS (SELECT 1 FROM match_decisions d WHERE d.match_run_id = last_run.id
                                                            AND 'below_daily_cap' = ANY (d.reason_codes)))
        ORDER BY u.id"""
)
NO_RUN_YET = text(
    """SELECT u.id FROM users u
        WHERE u.status = 'active' AND u.record_confirmed_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM match_runs r WHERE r.user_id = u.id)
        ORDER BY u.record_confirmed_at, u.id"""
)


class PacingWait(Exception):
    """The llm queue is behind: the plan fails the retryable way and the queue tries it again after its backoff."""


def _enqueue_run(conn: Connection, kind: str, payload: dict[str, Any], *, unique_key: str, queue: str) -> bool:
    """Queue a run (False when its key is already taken), with a lease long enough for it to finish."""
    job_id = enqueue(conn, kind, payload, unique_key=unique_key, queue=queue)
    if job_id is None:
        return False
    conn.execute(
        text("UPDATE queue_jobs SET lease_seconds = :s WHERE id = :id"), {"s": RUN_LEASE_SECONDS, "id": job_id}
    )
    return True


def plan_zones(conn: Connection, now: datetime) -> int:
    """Queue match.plan for every time zone with people to match where local time is at or after NIGHTLY_START, once per
    zone and local date; returns how many were new. A zone whose name this machine cannot read is skipped."""
    zones = conn.execute(
        text(
            """SELECT DISTINCT timezone FROM users
                WHERE status = 'active' AND brief_frequency <> 'paused' AND record_confirmed_at IS NOT NULL
                ORDER BY timezone"""
        )
    ).scalars()
    queued = 0
    for zone in zones:
        try:
            local = now.astimezone(ZoneInfo(zone))
        except (KeyError, ValueError, OSError):  # not an IANA name this machine knows
            log.event("match.zone_unreadable", level=logging.WARNING)
            continue
        if local.time() < NIGHTLY_START:
            continue
        day = local.date().isoformat()
        job_id = enqueue(
            conn,
            "match.plan",
            {"zone": zone, "local_date": day},
            unique_key=f"match.plan:{zone}:{day}",
            max_attempts=PLAN_ATTEMPTS,
        )
        queued += job_id is not None
    if queued:
        log.event("match.zones", planned=queued)
    return queued


def plan_zone(conn: Connection, zone: str, day: date, now: datetime) -> int:
    """Queue match.nightly for each person of the zone who is due on `day`; returns how many were new. Raises PacingWait
    while the oldest job waiting on the llm queue has waited longer than PACING_LIMIT."""
    oldest = conn.execute(text("SELECT min(run_at) FROM queue_jobs WHERE queue = 'llm' AND status = 'queued'")).scalar()
    if oldest is not None and now - oldest > PACING_LIMIT:
        raise PacingWait
    due = conn.execute(DUE_TONIGHT, {"zone": zone, "day": day}).scalars().all()
    queued = 0
    for user_id in due:
        payload = {"user_id": str(user_id), "local_date": day.isoformat()}
        queued += _enqueue_run(conn, "match.nightly", payload, unique_key=nightly_key(user_id, day), queue="llm")
    log.event("match.plan", due=len(due), planned=queued)
    return queued


def sweep_onboarding(conn: Connection) -> int:
    """Queue a first run (trigger onboarding) for everyone with a confirmed record and no run yet; returns how many were
    new. Nothing announces a confirmed record (Phase 3 built no event bus), so the sweep finds them."""
    queued = 0
    for user_id in conn.execute(NO_RUN_YET).scalars().all():
        payload = {"user_id": str(user_id), "trigger": "onboarding"}
        queued += _enqueue_run(
            conn, "match.run", payload, unique_key=f"match.onboarding:{user_id}", queue="interactive"
        )
    if queued:
        log.event("match.onboarding_sweep", planned=queued)
    return queued


def _run(payload: dict[str, Any], trigger: str) -> None:
    """Run the match for the person the job names. A provider outage or a database hiccup fails the job the retryable
    way; a person who is gone is skipped; anything else is a permanent failure (retrying the same input cannot help)."""
    day = date.fromisoformat(payload["local_date"]) if "local_date" in payload else None
    try:
        service.run(UUID(payload["user_id"]), trigger=trigger, local_date=day)
    except service.NotRunnable:
        log.event("match.skipped", kind=trigger)  # suspended or deleted after the job was queued
    except (ProviderUnavailable, OperationalError):
        raise
    except Exception as error:
        raise NonRetryable from error


@every("match.zones", SWEEP_SECONDS)
def zones(conn: Connection, payload: dict[str, Any]) -> None:
    plan_zones(conn, datetime.now(timezone.utc))


@handler("match.plan")
def plan(conn: Connection, payload: dict[str, Any]) -> None:
    plan_zone(conn, payload["zone"], date.fromisoformat(payload["local_date"]), datetime.now(timezone.utc))


@handler("match.nightly")
def nightly(conn: Connection, payload: dict[str, Any]) -> None:
    _run(payload, "nightly")


@every("match.onboarding_sweep", SWEEP_SECONDS)
def onboarding_sweep(conn: Connection, payload: dict[str, Any]) -> None:
    sweep_onboarding(conn)


@handler("match.run")
def run_now(conn: Connection, payload: dict[str, Any]) -> None:
    _run(payload, payload["trigger"])
