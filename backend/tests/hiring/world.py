"""Invented rows for the hiring-desk database tests (plan 06-01): employers, their members, roles on an unlisted job, a
candidate who is open to introductions, and the worker's ask.

Everything here is made up: the organizations end in "Demo", every address is example.test, and the postings are the
"Halden Demo" ones. The functions write with plain SQL on the connection they are given, which is the test's own
bootstrap connection (it bypasses row-level security, and the `conn` fixture rolls its transaction back); nothing
here commits. The guards of the schema still run, so a row built wrongly fails here and not in silence.

A test imports it by its package path (backend/ is on the import path and tests/ has no __init__.py):
    from tests.hiring.world import make_org, open_role, candidate, ask, accept
"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.match.factories import Job, make_decision, make_job, make_user

DESCRIPTION = "Python is required. SQL is a plus."
REQUIREMENTS = [("Python is required.", "required"), ("SQL is a plus.", "preferred")]
# What the candidate agreed to share (API.md 3.12), invented.
PREVIEW = {
    "on_accept": {"name": "Demo Candidate", "headline": "Data engineer", "evidence": [], "facts": []},
    "on_employer_yes": {"email": "demo.candidate@example.test"},
}


@dataclass(frozen=True)
class Role:
    """An open role: its row, its organization and the unlisted job it is published as."""

    id: UUID
    org: UUID
    job: Job


def make_org(
    conn: Connection, name: str = "Halden Demo", *, timezone: str = "UTC", created_by: UUID | None = None
) -> UUID:
    return conn.execute(
        text("INSERT INTO organizations (name, timezone, created_by) VALUES (:n, :tz, :c) RETURNING id"),
        {"n": name, "tz": timezone, "c": created_by},
    ).scalar_one()


def add_member(conn: Connection, org: UUID, user: UUID, role: str = "member") -> UUID:
    """An active member, as create_organization or an accepted invitation leaves one."""
    return conn.execute(
        text(
            """INSERT INTO org_members (organization_id, user_id, role, status, joined_at)
               VALUES (:o, :u, CAST(:r AS org_role), 'active', now()) RETURNING id"""
        ),
        {"o": org, "u": user, "r": role},
    ).scalar_one()


def invite(conn: Connection, org: UUID, email: str, role: str = "member", invited_by: UUID | None = None) -> UUID:
    """A pending invitation: an address and no user yet."""
    return conn.execute(
        text(
            """INSERT INTO org_members (organization_id, invited_email, role, invited_by)
               VALUES (:o, :e, CAST(:r AS org_role), :b) RETURNING id"""
        ),
        {"o": org, "e": email, "r": role, "b": invited_by},
    ).scalar_one()


def draft_role(conn: Connection, org: UUID, title: str = "Draft role") -> UUID:
    return conn.execute(
        text("INSERT INTO roles (organization_id, title, description) VALUES (:o, :t, :d) RETURNING id"),
        {"o": org, "t": title, "d": DESCRIPTION},
    ).scalar_one()


def open_role(
    conn: Connection,
    org: UUID,
    *,
    title: str = "Data engineer",
    name: str = "Halden Demo",
    created_by: UUID | None = None,
) -> Role:
    """A live role: confirmed requirements, a pay range, and its posting as an unlisted job of the same company name."""
    job = make_job(conn, title=title, description=DESCRIPTION, requirements=REQUIREMENTS, company_name=name)
    conn.execute(text("UPDATE jobs SET is_listed = false WHERE id = :j"), {"j": job.id})
    role = conn.execute(
        text(
            """INSERT INTO roles (organization_id, title, description, status, salary_min, salary_max, salary_currency,
                                  requirements_confirmed_at, job_id, created_by, opened_at)
               VALUES (:o, :t, :d, 'open', 80000, 120000, 'USD', now(), :j, :c, now()) RETURNING id"""
        ),
        {"o": org, "t": title, "d": DESCRIPTION, "j": job.id, "c": created_by},
    ).scalar_one()
    for ordinal, (quote, importance) in enumerate(REQUIREMENTS, start=1):
        start = DESCRIPTION.find(quote)
        conn.execute(
            text(
                """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text, span,
                                                  is_confirmed)
                   VALUES (:r, :o, :n, CAST(:i AS requirement_importance), 'skill', :t, int4range(:a, :b), true)"""
            ),
            {"r": role, "o": org, "n": ordinal, "i": importance, "t": quote, "a": start, "b": start + len(quote)},
        )
    return Role(role, org, job)


def grant(conn: Connection, user: UUID, purpose: str, *, org: UUID | None = None, role: UUID | None = None) -> UUID:
    """A consent grant. Purpose 'introduction' names its organization and role, as the table requires."""
    return conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, text_version, organization_id, role_id)
               VALUES (:u, CAST(:p AS consent_purpose), 'granted', 'test-v1', :o, :r) RETURNING id"""
        ),
        {"u": user, "p": purpose, "o": org, "r": role},
    ).scalar_one()


def withdraw(conn: Connection, grant_id: UUID) -> None:
    """The withdrawal row that ends a grant (consents is append-only: nothing is updated)."""
    conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version)
               SELECT user_id, purpose, 'withdrawn', id, text_version FROM consents WHERE id = :g"""
        ),
        {"g": grant_id},
    )


def candidate(conn: Connection, *, open_to_introductions: bool = True) -> UUID:
    """An invented account, open to introductions unless told otherwise."""
    user = make_user(conn)
    if open_to_introductions:
        grant(conn, user, "open_to_introductions")
    return user


def role_run(conn: Connection, role: Role) -> UUID:
    """A finished role run: trigger 'role', the role's id, no person."""
    return conn.execute(
        text(
            """INSERT INTO match_runs (trigger, role_id, local_date, status, ranker_version, finished_at)
               VALUES ('role', :r, current_date, 'completed', 'test-ranker-v1', now()) RETURNING id"""
        ),
        {"r": role.id},
    ).scalar_one()


def introduced(
    conn: Connection, role: Role, user: UUID, *, run: UUID | None = None, outcome: str = "introduced"
) -> UUID:
    """The decision a role run records for a candidate: introduced (the ask may follow) or withheld."""
    withheld = outcome == "withheld"
    return make_decision(
        conn,
        run or role_run(conn, role),
        user,
        role.job,
        outcome=outcome,
        reasons=("required_not_met",) if withheld else ("selected_for_role",),
        summary=None if withheld else "Meets every required item.",
    )


def insert_introduction(
    conn: Connection,
    role: Role,
    user: UUID,
    decision: UUID,
    *,
    asked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> UUID:
    """The bare INSERT of an ask, as whichever role the connection has taken. The guard checks it."""
    return conn.execute(
        text(
            """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview, asked_at,
                                          expires_at)
               VALUES (:o, :r, :u, :d, CAST(:p AS jsonb), COALESCE(CAST(:a AS timestamptz), now()),
                       CAST(:e AS timestamptz)) RETURNING id"""
        ),
        {
            "o": role.org,
            "r": role.id,
            "u": user,
            "d": decision,
            "p": json.dumps(PREVIEW),
            "a": asked_at,
            "e": expires_at,
        },
    ).scalar_one()


def ask(conn: Connection, role: Role, user: UUID, *, run: UUID | None = None, asked_at: datetime | None = None) -> UUID:
    """The worker's ask: an introduced decision for the candidate and the role's job, then the introduction row."""
    return insert_introduction(conn, role, user, introduced(conn, role, user, run=run), asked_at=asked_at)


def exclude_company(conn: Connection, user: UUID, company: str) -> None:
    """The candidate's hard constraint 'never this employer' (HIR-07), through the record's own change-log row."""
    event = conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', 'constraint', 'create', '{}'::jsonb, 'candidate_edit', 1) RETURNING id"""
        ),
        {"u": user},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO constraints (user_id, kind, statement, company_names, source, profile_event_id)
               VALUES (:u, 'excluded_company', :s, CAST(:n AS text[]), 'candidate_edit', :e)"""
        ),
        {"u": user, "s": f"Never {company}.", "n": [company], "e": event},
    )


def accept(conn: Connection, intro: UUID, role: Role, user: UUID) -> UUID:
    """The candidate's yes: the scoped consent, then the status move. Returns the consent grant, to be withdrawn."""
    consent = grant(conn, user, "introduction", org=role.org, role=role.id)
    conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    return consent


def shortlist(conn: Connection, intro: UUID) -> UUID:
    """The employer's frozen copy of an accepted introduction."""
    return conn.execute(
        text(
            """INSERT INTO shortlist_entries (organization_id, role_id, introduction_id, user_id, shared_payload,
                                              contact_released_at)
               SELECT organization_id, role_id, id, user_id, share_preview, now() FROM introductions WHERE id = :i
               RETURNING id"""
        ),
        {"i": intro},
    ).scalar_one()


def signal(
    conn: Connection,
    side: str,
    user: UUID,
    org: UUID,
    role: UUID,
    *,
    intro: UUID | None = None,
    sent_by: UUID | None = None,
    slot: int = 1,
    created_at: datetime | None = None,
) -> UUID:
    """A serious-interest signal. Its week is the UTC Monday of created_at, as the table's own check requires."""
    return conn.execute(
        text(
            """INSERT INTO interest_signals (sender_side, user_id, organization_id, role_id, introduction_id, sent_by,
                                             week_start, slot, created_at)
               VALUES (CAST(:side AS signal_side), :u, :o, :r, :i, :s,
                       date_trunc('week', COALESCE(CAST(:c AS timestamptz), now()) AT TIME ZONE 'UTC')::date, :slot,
                       COALESCE(CAST(:c AS timestamptz), now()))
               RETURNING id"""
        ),
        {"side": side, "u": user, "o": org, "r": role, "i": intro, "s": sent_by, "slot": slot, "c": created_at},
    ).scalar_one()


# ---- what the database tests do with those rows ---------------------------------------------------------------


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def sqlstate(caught) -> str:
    return caught.value.orig.sqlstate


def message(caught) -> str:
    return caught.value.orig.diag.message_primary


def constraint_name(caught) -> str:
    return caught.value.orig.diag.constraint_name


def act_as(conn: Connection, role: str, user: UUID | None = None, *, token: UUID | None = None) -> None:
    """Drop the bootstrap superuser's privileges for the rest of the test transaction, as a request does. A token id
    makes it an outside agent's request, which no hiring policy opens anything to."""
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if user else ""})
    conn.execute(text("SELECT set_config('app.agent_token_id', :t, true)"), {"t": str(token) if token else ""})


def count(conn: Connection, sql: str, **params) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {sql}"), params).scalar_one()
