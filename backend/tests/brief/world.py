"""A person with a record, a consent and shown matches: the starting point of most brief tests (plan 04-05).

Importable as `from tests.brief.world import world` (tests/ has no __init__.py, so the folders are namespace packages).
It writes on the connection it is given: pass the `conn` fixture for a test that rolls back, or a connection from
`engine.begin()` for one that goes through the real database roles. Everything is invented.
"""

from sqlalchemy import Connection, text

from app.trust import api as trust
from tests.match.factories import Verdict, make_job, make_record, make_run, make_shown_decision, make_user


def world(conn: Connection, count: int = 3, *, consent: bool = True, email: str | None = None, prefix: str = ""):
    """A person with a confirmed record, the brief_email consent (unless left out) and `count` matches shown for
    2026-11-23, ranked 1 to `count`: "Role 1" at "Initech Demo", external id "job-1", and so on. A second person in
    one test needs a `prefix`, which goes before the external ids, because those are unique. Returns the person's id
    and the decisions' ids in rank order."""
    user = make_user(conn, email=email)
    make_record(conn, user)
    if consent:
        trust.grant_consent(conn, user, "brief_email", "test-wording")
    run = make_run(conn, user)
    decisions = []
    for rank in range(1, count + 1):
        job = make_job(
            conn,
            title=f"Role {rank}",
            description="Python.",
            requirements=[("Python.", "required")],
            external_id=f"{prefix}job-{rank}",
        )
        decisions.append(
            make_shown_decision(
                conn, run, user, job, [Verdict("no_evidence")], rank_position=rank, summary=f"Summary {rank}."
            )
        )
    return user, decisions


def count(conn: Connection, table: str, **where) -> int:
    """How many rows of `table` match the column = value pairs (table and column names are the tests' own)."""
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "true"
    return int(conn.execute(text(f"SELECT count(*) FROM {table} WHERE {clause}"), where).scalar_one())


def everything(conn: Connection, user) -> tuple[int, int, int]:
    """The rows a compose writes for a person, counted: briefs, items and outbox rows."""
    return (
        count(conn, "briefs", user_id=user),
        count(conn, "brief_items", user_id=user),
        count(conn, "outbox", user_id=user),
    )
