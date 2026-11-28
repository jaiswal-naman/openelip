"""What the wind tunnel refuses before it writes a row (threats T-08-19 and T-08-22; INDEX decisions 5 and 37).

The tunnel creates accounts, campaigns and mail, drains the whole job queue and relays the whole outbox, so it runs only
on a scratch database that nobody else is using.
"""

import re
from datetime import timedelta

from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url

from app.platform import config, db
from app.platform.config import Role

TEST_NAME = re.compile(r"[a-z0-9_]+_test")
LOGINS: tuple[Role, ...] = ("migrate", "api", "worker", "match")
OTHER_WORKER_WINDOW = timedelta(minutes=2)  # assumption: every worker's scheduler creates a queue.reap job each minute


class NotATestDatabase(Exception):
    """A login names, or reaches, a database whose name does not end in _test (or the logins disagree)."""


class WorkerAttached(Exception):
    """Another worker is running jobs on this database; it would claim the tunnel's jobs and mail its messages."""


class DatabaseBusy(Exception):
    """A real person has queued jobs or pending mail here; the tunnel would drain and relay them."""


def require_test_database() -> str:
    """All four logins name one database ending in _test, and the server says each of them is connected to it.

    The URL's path is not the whole story (a `dbname` query parameter overrides it), so every login is asked for
    current_database(). Raised before any row is written; returns the database's name.
    """
    settings = config.get()
    named = {role: make_url(settings.database_url(role)).database or "" for role in LOGINS}
    for role, name in named.items():
        if not TEST_NAME.fullmatch(name):
            raise NotATestDatabase(f"the {role} login names database {name!r}, which does not end in _test")
    if len(set(named.values())) != 1:
        raise NotATestDatabase("the four database logins name different databases")
    (expected,) = set(named.values())
    for role in LOGINS:
        with db.engine(role).connect() as conn:
            reached = str(conn.execute(text("SELECT current_database()")).scalar_one())
        if reached != expected or not TEST_NAME.fullmatch(reached):
            raise NotATestDatabase(
                f"the {role} login is connected to database {reached!r}, not to {expected!r} or not a _test database"
            )
    return expected


def require_no_other_worker(conn: Connection, worker_id: str) -> None:
    """No job is running under another worker's name, and no scheduler `queue.reap` job is recent.

    A finished job no longer names its worker (the queue clears locked_by when a job ends), but every worker's
    scheduler creates one `queue.reap:<slot>` job a minute, so a recent one means a worker is attached.
    """
    running = conn.execute(
        text("SELECT count(*) FROM queue_jobs WHERE status = 'running' AND locked_by IS DISTINCT FROM :me"),
        {"me": worker_id},
    ).scalar_one()
    recent = conn.execute(
        text(
            """SELECT count(*) FROM queue_jobs
                WHERE unique_key LIKE 'queue.reap:%' AND created_at > now() - :window"""
        ),
        {"window": OTHER_WORKER_WINDOW},
    ).scalar_one()
    if running or recent:
        raise WorkerAttached("another worker is attached to this database; stop it and wait two minutes")


def require_quiet_database(conn: Connection) -> None:
    """Every queued or running job and every pending or sending outbox row belongs to a synthetic person."""
    busy = conn.execute(
        text(
            """SELECT (SELECT count(*) FROM queue_jobs j LEFT JOIN users u ON u.id = j.user_id
                        WHERE j.status IN ('queued', 'running') AND u.is_synthetic IS NOT TRUE)
                    + (SELECT count(*) FROM outbox o LEFT JOIN users u ON u.id = o.user_id
                        WHERE o.status IN ('pending', 'sending') AND u.is_synthetic IS NOT TRUE)"""
        )
    ).scalar_one()
    if busy:
        raise DatabaseBusy("a person's queued job or pending mail is on this database; the tunnel needs a quiet one")
