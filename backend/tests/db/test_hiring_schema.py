"""Revision p6_0601_hiring: what the database enforces on the hiring desk by itself (plan 06-01).

These rules hold whatever the application does: the pay-range gate (HIR-02), who may be asked (invariants 1 and 4, the
blocked employer of HIR-07, the answer clock of HIR-06), seven days of silence (HIR-04), one ask per role and candidate
(invariant 6), one employer answer with a reason (HIR-06), a shortlist entry only for an accepted introduction
(HIR-05), and the weekly signal limit (HIR-08, owner decision G7). Most tests run in the `conn` fixture's transaction on
the main test database (already at head), which is rolled back; one runs the revision's own downgrade on a scratch
database. Every row is invented (tests/hiring/world.py).
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text

from tests.hiring.world import (
    PREVIEW,
    Role,
    accept,
    act_as,
    add_member,
    ask,
    candidate,
    constraint_name,
    count,
    draft_role,
    exclude_company,
    grant,
    insert_introduction,
    introduced,
    make_org,
    message,
    open_role,
    refused,
    role_run,
    shortlist,
    signal,
    sqlstate,
    withdraw,
)
from tests.match.factories import make_decision, make_job, make_user

REPO = Path(__file__).resolve().parents[3]
REVISION = "p6_0601_hiring"
PREVIOUS = "p4_0401_match_brief"
NEW_ENUMS = ("org_role", "member_status", "role_status", "introduction_status", "employer_answer", "signal_side")
NEW_TABLES = (
    "organizations",
    "org_members",
    "roles",
    "role_requirements",
    "introductions",
    "shortlist_entries",
    "interest_signals",
)
NEW_FUNCTIONS = (
    "add_business_days",
    "consent_is_active",
    "has_active_consent",
    "organization_has_overdue_answers",
    "app_is_org_member",
    "app_is_org_admin",
    "introductions_guard",
    "shortlist_entries_check",
    "interest_signals_check",
    "create_organization",
    "accept_org_invitation",
    "org_directory",
    "suppress_small",
    "role_preview",
    "my_org_invitations",
    "introduction_role",
)
# DATA_MODEL 3.14: the foreign keys to the new tables, from tables that already existed: (table, target, ON DELETE).
LATE_KEYS = {
    "consents_organization_id_fkey": ("consents", "organizations", "c"),
    "consents_role_id_fkey": ("consents", "roles", "c"),
    "audit_log_organization_id_fkey": ("audit_log", "organizations", "n"),
    "job_sources_organization_id_fkey": ("job_sources", "organizations", "a"),
    "match_runs_role_id_fkey": ("match_runs", "roles", "c"),
    "brief_items_introduction_id_fkey": ("brief_items", "introductions", "c"),
}


def names(conn: Connection, sql: str, **params) -> list:
    return list(conn.execute(text(sql), params).scalars())


@dataclass(frozen=True)
class Desk:
    org: UUID
    owner: UUID
    role: Role  # open, with two requirements
    cat: UUID  # a candidate who is open to introductions


@pytest.fixture
def desk(conn: Connection) -> Desk:
    owner = make_user(conn)
    org = make_org(conn, created_by=owner)
    add_member(conn, org, owner, "owner")
    return Desk(org, owner, open_role(conn, org, created_by=owner), candidate(conn))


def worker_inserts(conn: Connection, desk: Desk, user: UUID, decision: UUID, **fields) -> UUID:
    """The ask as the worker makes it: the only role that may insert an introduction."""
    act_as(conn, "app_worker")
    try:
        return insert_introduction(conn, desk.role, user, decision, **fields)
    finally:
        conn.execute(text("RESET ROLE"))


def make_overdue(conn: Connection, intro: UUID) -> None:
    """Move an accepted introduction's answer time into the past. The guard allows it only under app.maintenance."""
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("UPDATE introductions SET answer_due_at = now() - interval '1 day' WHERE id = :i"), {"i": intro})
    conn.execute(text("SELECT set_config('app.maintenance', 'off', true)"))


def intro_row(conn: Connection, intro: UUID):
    return conn.execute(
        text(
            """SELECT status::text AS status, responded_at, answer_due_at, employer_answer::text AS answer,
                      employer_answered_at, now() AS now FROM introductions WHERE id = :i"""
        ),
        {"i": intro},
    ).one()


AUDIT_ROW = """INSERT INTO audit_log (event_type, actor_type, organization_id)
               VALUES ('hiring.role_closed', 'employer', :o)"""
BRIEF_ITEM = """INSERT INTO brief_items (brief_id, position, user_id, kind, introduction_id)
                VALUES (:b, :p, :u, 'introduction', :i)"""


def brief_listing(conn: Connection, user: UUID, intro: UUID) -> UUID:
    """An in-app brief of the candidate with one item: the introduction. Returns the brief's id."""
    brief = conn.execute(
        text("INSERT INTO briefs (user_id, local_date, channel) VALUES (:u, current_date, 'in_app') RETURNING id"),
        {"u": user},
    ).scalar_one()
    conn.execute(text(BRIEF_ITEM), {"b": brief, "p": 1, "u": user, "i": intro})
    return brief


# ---- HIR-02: a role cannot go live without pay and confirmed requirements ---------------------------------------

PIECES = {
    "salary_min": "80000",
    "salary_max": "120000",
    "salary_currency": "'USD'",
    "requirements_confirmed_at": "now()",
    "job_id": ":job",
    "opened_at": "now()",
}


def go_live(conn: Connection, role: UUID, job: int, missing: str | None = None) -> None:
    """Make a draft role open, with every piece the CHECK wants except the one named, which is left NULL."""
    pieces = ", ".join(f"{column} = {'NULL' if column == missing else value}" for column, value in PIECES.items())
    conn.execute(text(f"UPDATE roles SET status = 'open', {pieces} WHERE id = :r"), {"r": role, "job": job})


def a_job(conn: Connection):
    return make_job(
        conn, title="Analyst", description="Excel is required.", requirements=[("Excel is required.", "required")]
    )


def test_a_role_with_every_piece_goes_live(conn, desk):
    draft = draft_role(conn, desk.org)
    go_live(conn, draft, a_job(conn).id)
    assert names(conn, "SELECT status::text FROM roles WHERE id = :r", r=draft) == ["open"]


@pytest.mark.parametrize("missing", list(PIECES))
def test_a_role_cannot_go_live_without_pay_confirmed_requirements_a_job_and_an_open_time(conn, desk, missing):
    draft = draft_role(conn, desk.org)
    with refused(conn) as caught:
        go_live(conn, draft, a_job(conn).id, missing)
    assert sqlstate(caught) == "23514" and constraint_name(caught) == "roles_live_needs_pay_and_requirements"


def test_a_pay_range_cannot_run_backwards_and_a_draft_may_have_none(conn, desk):
    draft = draft_role(conn, desk.org)  # no pay at all: fine while it is a draft
    with refused(conn) as caught:
        conn.execute(text("UPDATE roles SET salary_min = 90000, salary_max = 80000 WHERE id = :r"), {"r": draft})
    assert sqlstate(caught) == "23514" and constraint_name(caught) == "roles_check"  # the one unnamed check of roles


# ---- the answer clock's calendar ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "days", "zone", "expected"),
    [
        ("2026-11-27 10:00+05:30", 5, "Asia/Kolkata", "2026-12-04 10:00+05:30"),  # a Friday: the next Friday, same time
        ("2026-11-25 10:00+00", 2, "UTC", "2026-11-27 10:00+00"),  # Wednesday to Friday
        ("2026-11-27 10:00+00", 1, "UTC", "2026-11-30 10:00+00"),  # Friday to Monday: the weekend is not counted
        ("2026-11-28 10:00+00", 1, "UTC", "2026-11-30 10:00+00"),  # Saturday to Monday
        ("2026-11-27 23:00+00", 1, "Asia/Kolkata", "2026-11-29 23:00+00"),  # already Saturday there: Monday 04:30
    ],
)
def test_add_business_days_counts_monday_to_friday_in_the_given_time_zone(conn, start, days, zone, expected):
    got = conn.execute(
        text("SELECT add_business_days(CAST(:s AS timestamptz), :d, :z) = CAST(:e AS timestamptz)"),
        {"s": start, "d": days, "z": zone, "e": expected},
    ).scalar_one()
    assert got is True


# ---- who may be asked ---------------------------------------------------------------------------------------------


def test_the_worker_asks_and_the_row_expires_seven_days_later_whatever_the_caller_passed(conn, desk):
    decision = introduced(conn, desk.role, desk.cat)
    intro = worker_inserts(conn, desk, desk.cat, decision, expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    row = conn.execute(
        text("SELECT status::text, expires_at = asked_at + interval '7 days' FROM introductions WHERE id = :i"),
        {"i": intro},
    ).one()
    assert row == ("asked", True)


def test_the_api_role_cannot_insert_an_introduction_at_all(conn, desk):
    decision = introduced(conn, desk.role, desk.cat)
    act_as(conn, "app_api", desk.cat)
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, decision)
    assert sqlstate(caught) == "42501" and message(caught) == "permission denied for table introductions"


@pytest.mark.parametrize("case", ["withheld", "another candidate's", "another job's"])
def test_an_ask_needs_an_introduced_decision_for_this_candidate_and_the_roles_job(conn, desk, case):
    other = candidate(conn)
    decision = {
        "withheld": lambda: introduced(conn, desk.role, desk.cat, outcome="withheld"),
        "another candidate's": lambda: introduced(conn, desk.role, other),
        "another job's": lambda: make_decision(
            conn,
            role_run(conn, desk.role),
            desk.cat,
            a_job(conn),
            outcome="introduced",
            reasons=("selected_for_role",),
            summary="Meets every required item.",
        ),
    }[case]()
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, decision)
    assert sqlstate(caught) == "23514"
    assert message(caught) == "introductions need an introduced match decision for this candidate and role"


@pytest.mark.parametrize("status", ["draft", "closed"])
def test_a_role_that_is_not_open_asks_nobody(conn, desk, status):
    decision = introduced(conn, desk.role, desk.cat)
    conn.execute(
        text("UPDATE roles SET status = CAST(:s AS role_status) WHERE id = :r"), {"s": status, "r": desk.role.id}
    )
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, decision)
    assert sqlstate(caught) == "23514" and message(caught) == "introductions need an open role of this organization"


@pytest.mark.parametrize("withdrawn", [False, True], ids=["never agreed", "agreed then withdrew"])
def test_a_candidate_who_is_not_open_to_introductions_is_not_asked(conn, desk, withdrawn):
    shy = candidate(conn, open_to_introductions=False)
    if withdrawn:
        withdraw(conn, grant(conn, shy, "open_to_introductions"))
    decision = introduced(conn, desk.role, shy)
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, shy, decision)
    assert sqlstate(caught) == "23514" and message(caught).startswith("consent_missing")


@pytest.mark.parametrize("spelling", ["Halden Demo", "halden demo, inc."])
def test_a_blocked_employer_cannot_ask_however_the_name_is_spelled(conn, desk, spelling):
    exclude_company(conn, desk.cat, spelling)  # HIR-07: the candidate named this employer in advance
    decision = introduced(conn, desk.role, desk.cat)
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, decision)
    assert sqlstate(caught) == "23514" and message(caught).startswith("hard_constraint")


def test_an_organization_with_an_overdue_answer_asks_nobody_until_it_answers(conn, desk):
    waiting = candidate(conn)
    owed = ask(conn, desk.role, waiting)
    accept(conn, owed, desk.role, waiting)
    assert conn.execute(text("SELECT organization_has_overdue_answers(:o)"), {"o": desk.org}).scalar_one() is False
    make_overdue(conn, owed)
    assert conn.execute(text("SELECT organization_has_overdue_answers(:o)"), {"o": desk.org}).scalar_one() is True

    decision = introduced(conn, desk.role, desk.cat)
    act_as(conn, "app_worker")
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, decision)
    assert sqlstate(caught) == "23514" and message(caught).startswith("answer_clock")

    conn.execute(text("RESET ROLE"))  # the employer answers: the pause ends
    conn.execute(
        text("UPDATE introductions SET employer_answer = 'no', employer_answer_reason = 'Role filled' WHERE id = :i"),
        {"i": owed},
    )
    assert worker_inserts(conn, desk, desk.cat, decision) is not None


def test_a_second_ask_for_the_same_role_and_candidate_is_refused_by_the_unique_key(conn, desk):
    ask(conn, desk.role, desk.cat)
    again = introduced(conn, desk.role, desk.cat)  # a later run introduces the same candidate again
    with refused(conn) as caught:
        insert_introduction(conn, desk.role, desk.cat, again)
    assert sqlstate(caught) == "23505" and constraint_name(caught) == "introductions_once"


# ---- what happens to an ask: accept, decline, expire --------------------------------------------------------------


def test_accepting_needs_the_candidates_consent_for_this_employer_and_this_role(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    assert sqlstate(caught) == "23514" and message(caught).startswith("consent_missing")
    grant(conn, desk.cat, "introduction", org=desk.org, role=draft_role(conn, desk.org))  # another role: no use
    with refused(conn) as caught:
        conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    assert message(caught).startswith("consent_missing")


def test_accepting_starts_the_answer_clock_in_the_organizations_time_zone(conn):
    owner = make_user(conn)
    org = make_org(conn, "Chennai Demo", timezone="Asia/Kolkata", created_by=owner)
    role = open_role(conn, org, name="Chennai Demo")
    cat = candidate(conn)
    intro = ask(conn, role, cat)
    accept(conn, intro, role, cat)
    row = conn.execute(
        text(
            """SELECT status::text, responded_at = now(), answer_due_at = add_business_days(now(), 5, 'Asia/Kolkata'),
                      answer_due_at <> add_business_days(now(), 5, 'UTC') FROM introductions WHERE id = :i"""
        ),
        {"i": intro},
    ).one()
    assert row == ("accepted", True, True, True)


def test_declining_records_the_time_and_starts_no_clock(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    conn.execute(text("UPDATE introductions SET status = 'declined' WHERE id = :i"), {"i": intro})
    row = intro_row(conn, intro)
    assert (row.status, row.responded_at == row.now, row.answer_due_at) == ("declined", True, None)


def test_silence_for_seven_days_counts_as_no_and_the_ask_cannot_be_accepted_late(conn, desk):
    old = datetime.now(timezone.utc) - timedelta(days=8)
    late = ask(conn, desk.role, desk.cat, asked_at=old)
    grant(conn, desk.cat, "introduction", org=desk.org, role=desk.role.id)
    with refused(conn) as caught:
        conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": late})
    assert sqlstate(caught) == "23514" and message(caught).startswith("introduction expired")
    conn.execute(text("UPDATE introductions SET status = 'expired' WHERE id = :i"), {"i": late})
    assert intro_row(conn, late).status == "expired"


def test_an_ask_cannot_be_marked_expired_before_its_seven_days_are_over(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(text("UPDATE introductions SET status = 'expired' WHERE id = :i"), {"i": intro})
    assert sqlstate(caught) == "23514" and message(caught) == "introduction has not expired yet"


@pytest.mark.parametrize("target", ["asked", "declined", "expired", "withdrawn"])
def test_an_accepted_introduction_never_moves_again(conn, desk, target):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE introductions SET status = CAST(:t AS introduction_status) WHERE id = :i"),
            {"t": target, "i": intro},
        )
    assert sqlstate(caught) == "23514" and message(caught) == f"introduction cannot move from accepted to {target}"


CHANGES = {
    "share_preview": "CAST('{\"on_accept\": {}}' AS jsonb)",
    "asked_at": "asked_at + interval '1 hour'",
    "expires_at": "expires_at + interval '1 day'",
    "created_at": "created_at - interval '1 day'",
    "user_id": "'00000000-0000-0000-0000-000000000001'",
    "organization_id": "'00000000-0000-0000-0000-000000000002'",
    "role_id": "'00000000-0000-0000-0000-000000000003'",
    "match_decision_id": "'00000000-0000-0000-0000-000000000004'",
}


@pytest.mark.parametrize("column", list(CHANGES))
def test_what_was_asked_and_what_was_shared_never_changes(conn, desk, column):
    intro = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(text(f"UPDATE introductions SET {column} = {CHANGES[column]} WHERE id = :i"), {"i": intro})
    assert sqlstate(caught) == "42501" and message(caught) == "introduction content is immutable"


def test_the_answer_time_is_set_once(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE introductions SET answer_due_at = answer_due_at + interval '1 day' WHERE id = :i"),
            {"i": intro},
        )
    assert sqlstate(caught) == "23514" and message(caught) == "answer_due_at is set once"


# ---- HIR-06: one answer, with a reason, only after acceptance -----------------------------------------------------

YES = "UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = 'Strong fit' WHERE id = :i"


def test_the_employer_cannot_answer_an_ask_the_candidate_has_not_accepted(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(text(YES), {"i": intro})
    assert sqlstate(caught) == "23514" and message(caught) == "the employer answers once, after acceptance"


@pytest.mark.parametrize("reason", ["'   '", "''", "NULL"], ids=["blank", "empty", "none"])
def test_an_answer_without_a_reason_is_refused(conn, desk, reason):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(
            text(f"UPDATE introductions SET employer_answer = 'no', employer_answer_reason = {reason} WHERE id = :i"),
            {"i": intro},
        )
    assert sqlstate(caught) == "23514" and "introductions_check" in constraint_name(caught)


def test_the_employer_answers_once_and_the_time_is_stamped_by_the_database(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    conn.execute(text(YES), {"i": intro})
    row = intro_row(conn, intro)
    assert (row.answer, row.employer_answered_at == row.now) == ("yes", True)
    with refused(conn) as caught:
        conn.execute(text(YES.replace("'yes'", "'no'").replace("Strong fit", "Changed my mind")), {"i": intro})
    assert sqlstate(caught) == "23514" and message(caught) == "the employer answers once, after acceptance"


# ---- HIR-05: the shortlist holds only what the candidate accepted -------------------------------------------------


def test_a_shortlist_entry_for_an_ask_nobody_accepted_is_refused(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        shortlist(conn, intro)
    assert sqlstate(caught) == "23514" and message(caught).startswith("consent_missing: shortlist entries need")


def test_a_shortlist_entry_must_carry_exactly_the_preview_the_candidate_saw(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    with refused(conn) as caught:
        conn.execute(
            text(
                """INSERT INTO shortlist_entries (organization_id, role_id, introduction_id, user_id, shared_payload)
                   VALUES (:o, :r, :i, :u, CAST(:p AS jsonb))"""
            ),
            {
                "o": desk.org,
                "r": desk.role.id,
                "i": intro,
                "u": desk.cat,
                "p": '{"on_accept": {"name": "Someone else"}}',
            },
        )
    assert sqlstate(caught) == "23514" and message(caught).startswith("consent_missing: shortlist entries need")


def test_a_shortlist_entry_is_made_once_for_an_accepted_introduction(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    entry = shortlist(conn, intro)
    same = "SELECT shared_payload = CAST(:p AS jsonb) FROM shortlist_entries WHERE id = :e"
    assert names(conn, same, e=entry, p=json.dumps(PREVIEW)) == [True]
    with refused(conn) as caught:
        shortlist(conn, intro)  # one entry per introduction
    assert sqlstate(caught) == "23505"


def test_no_shortlist_entry_is_made_once_the_candidate_withdrew_the_consent(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    withdraw(conn, accept(conn, intro, desk.role, desk.cat))
    with refused(conn) as caught:
        shortlist(conn, intro)
    assert sqlstate(caught) == "23514" and message(caught) == "consent_missing: introduction consent withdrawn"


# ---- HIR-08: scarce signals, and no employer signal before the candidate accepts (G7) -----------------------------


def test_an_organization_signal_needs_an_accepted_introduction_of_its_own(conn, desk):
    asked = ask(conn, desk.role, desk.cat)
    with refused(conn) as caught:
        signal(conn, "organization", desk.cat, desk.org, desk.role.id, intro=asked, sent_by=desk.owner)
    assert sqlstate(caught) == "23514" and message(caught) == "an organization signal must ride on its own introduction"
    accept(conn, asked, desk.role, desk.cat)
    signal(conn, "organization", desk.cat, desk.org, desk.role.id, intro=asked, sent_by=desk.owner)  # now it may
    other_org = make_org(conn, "Birch Demo")
    with refused(conn) as caught:  # an introduction of another organization is no ride
        signal(conn, "organization", desk.cat, other_org, desk.role.id, intro=asked, sent_by=desk.owner, slot=2)
    assert message(caught) == "an organization signal must ride on its own introduction"


def test_a_candidate_signals_three_times_a_week_and_a_fourth_finds_no_free_slot(conn, desk):
    roles = [draft_role(conn, desk.org, f"Role {n}") for n in range(4)]
    for slot, role in enumerate(roles[:3], start=1):
        signal(conn, "candidate", desk.cat, desk.org, role, sent_by=desk.cat, slot=slot)
    with refused(conn) as caught:  # every slot of the week is taken
        signal(conn, "candidate", desk.cat, desk.org, roles[3], sent_by=desk.cat, slot=3)
    assert sqlstate(caught) == "23505" and constraint_name(caught) == "interest_signals_candidate_week"
    with refused(conn) as caught:  # and there is no slot 4
        signal(conn, "candidate", desk.cat, desk.org, roles[3], sent_by=desk.cat, slot=4)
    assert sqlstate(caught) == "23514" and constraint_name(caught) == "interest_signals_slot_check"


def test_the_next_week_has_its_own_slots(conn, desk):
    roles = [draft_role(conn, desk.org, f"Role {n}") for n in range(2)]
    signal(conn, "candidate", desk.cat, desk.org, roles[0], sent_by=desk.cat, slot=1)
    next_week = datetime.now(timezone.utc) + timedelta(days=7)
    signal(conn, "candidate", desk.cat, desk.org, roles[1], sent_by=desk.cat, slot=1, created_at=next_week)


def test_a_second_signal_from_one_side_for_one_role_is_refused(conn, desk):
    signal(conn, "candidate", desk.cat, desk.org, desk.role.id, sent_by=desk.cat, slot=1)
    with refused(conn) as caught:
        signal(conn, "candidate", desk.cat, desk.org, desk.role.id, sent_by=desk.cat, slot=2)
    assert sqlstate(caught) == "23505" and constraint_name(caught) == "interest_signals_once"


def test_an_organization_has_three_signal_slots_a_week_too(conn, desk):
    yes = []
    for _ in range(2):
        person = candidate(conn)
        intro = ask(conn, desk.role, person)
        accept(conn, intro, desk.role, person)
        yes.append((person, intro))
    (first, first_intro), (second, second_intro) = yes
    signal(conn, "organization", first, desk.org, desk.role.id, intro=first_intro, sent_by=desk.owner, slot=1)
    with refused(conn) as caught:
        signal(conn, "organization", second, desk.org, desk.role.id, intro=second_intro, sent_by=desk.owner, slot=1)
    assert sqlstate(caught) == "23505" and constraint_name(caught) == "interest_signals_organization_week"


def test_a_signals_week_is_the_utc_monday_of_the_day_it_was_sent(conn, desk):
    insert = """INSERT INTO interest_signals (sender_side, user_id, organization_id, role_id, sent_by, week_start, slot,
                                               created_at)
                VALUES ('candidate', :u, :o, :r, :u, :w, 1, TIMESTAMPTZ '2026-11-25 12:00+00')"""
    args = {"u": desk.cat, "o": desk.org, "r": desk.role.id}
    with refused(conn) as caught:  # 2026-11-25 is a Wednesday: its week starts on Monday the 23rd
        conn.execute(text(insert), {**args, "w": "2026-11-25"})
    assert sqlstate(caught) == "23514" and "interest_signals_check" in constraint_name(caught)
    conn.execute(text(insert), {**args, "w": "2026-11-23"})


def test_only_an_organization_signal_names_an_introduction(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    with refused(conn) as caught:
        signal(conn, "candidate", desk.cat, desk.org, desk.role.id, intro=intro, sent_by=desk.cat)
    assert sqlstate(caught) == "23514"
    with refused(conn) as caught:
        signal(conn, "organization", desk.cat, desk.org, desk.role.id, sent_by=desk.owner)
    assert sqlstate(caught) == "23514"


# ---- HIR-03: the anonymous preview --------------------------------------------------------------------------------


def preview(conn: Connection, desk: Desk) -> dict:
    """role_preview as the owner of the organization sees it."""
    act_as(conn, "app_api", desk.owner)
    try:
        return conn.execute(text("SELECT role_preview(:r)"), {"r": desk.role.id}).scalar_one()
    finally:
        conn.execute(text("RESET ROLE"))


def decisions(conn: Connection, desk: Desk, run: UUID, selected: int, held_back: int) -> None:
    """One role run's decisions: some introduced, the rest withheld, each for a candidate of its own."""
    for n in range(selected + held_back):
        person = candidate(conn, open_to_introductions=False)
        introduced(conn, desk.role, person, run=run, outcome="introduced" if n < selected else "withheld")


@pytest.mark.parametrize(
    ("selected", "held_back", "eligible", "to_ask"),
    [(0, 0, 0, 0), (1, 2, "fewer than 5", "fewer than 5"), (4, 1, 5, "fewer than 5"), (7, 0, 7, 7)],
)
def test_counts_below_five_read_fewer_than_five_and_zero_stays_zero(conn, desk, selected, held_back, eligible, to_ask):
    decisions(conn, desk, role_run(conn, desk.role), selected, held_back)
    counts = preview(conn, desk)
    assert counts["eligible_candidates"] == eligible and counts["selected_to_ask"] == to_ask


def test_replies_are_counted_by_what_the_candidates_did_and_yes_is_never_suppressed(conn, desk):
    people = [candidate(conn) for _ in range(5)]
    intros = [ask(conn, desk.role, person) for person in people]
    accept(conn, intros[0], desk.role, people[0])
    accept(conn, intros[1], desk.role, people[1])
    conn.execute(text("UPDATE introductions SET status = 'declined' WHERE id = :i"), {"i": intros[2]})
    counts = preview(conn, desk)
    assert counts["said_yes"] == 2  # two is two: it is already on the shortlist
    assert counts["waiting_for_reply"] == "fewer than 5" and counts["said_no_or_expired"] == "fewer than 5"
    assert set(counts) == {
        "eligible_candidates",
        "selected_to_ask",
        "waiting_for_reply",
        "said_yes",
        "said_no_or_expired",
        "as_of",
    }


def test_the_preview_says_when_the_latest_finished_run_ended(conn, desk):
    assert preview(conn, desk)["as_of"] is None  # no run yet
    older = role_run(conn, desk.role)
    newer = role_run(conn, desk.role)
    conn.execute(text("UPDATE match_runs SET started_at = now() - interval '2 days' WHERE id = :r"), {"r": older})
    conn.execute(text("UPDATE match_runs SET finished_at = now() - interval '3 hours' WHERE id = :r"), {"r": newer})
    conn.execute(  # a run that is still going does not count
        text(
            """INSERT INTO match_runs (trigger, role_id, local_date, ranker_version)
               VALUES ('role', :r, current_date, 'test-ranker-v1')"""
        ),
        {"r": desk.role.id},
    )
    as_of = preview(conn, desk)["as_of"]
    finished = names(conn, "SELECT finished_at FROM match_runs WHERE id = :r", r=newer)[0]
    assert datetime.fromisoformat(as_of) == finished


# ---- the late foreign keys (DATA_MODEL 3.14) ------------------------------------------------------------------------


def test_the_foreign_keys_the_hiring_tables_make_possible_exist_with_the_data_models_actions(conn):
    rows = conn.execute(
        text(
            """SELECT conname, conrelid::regclass::text, confrelid::regclass::text, confdeltype::text
               FROM pg_constraint WHERE contype = 'f' AND conname = ANY(:n)"""
        ),
        {"n": list(LATE_KEYS)},
    ).all()
    assert {r[0]: (r[1], r[2], r[3]) for r in rows} == LATE_KEYS


def test_a_consent_and_an_audit_row_cannot_name_an_organization_that_does_not_exist(conn, desk):
    absent = uuid4()
    with refused(conn) as caught:
        conn.execute(
            text(
                """INSERT INTO consents (user_id, purpose, action, text_version, organization_id, role_id)
                   VALUES (:u, 'introduction', 'granted', 'v', :a, :a)"""
            ),
            {"u": desk.cat, "a": absent},
        )
    assert sqlstate(caught) == "23503" and constraint_name(caught) == "consents_organization_id_fkey"
    with refused(conn) as caught:
        conn.execute(text(AUDIT_ROW), {"o": absent})
    assert constraint_name(caught) == "audit_log_organization_id_fkey"


def test_a_direct_job_source_belongs_to_an_organization_that_exists(conn, desk):
    insert = """INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
                VALUES ('direct', :t, 'Halden Demo', :o, false)"""
    conn.execute(text(insert), {"t": f"org-{desk.org}", "o": desk.org})
    with refused(conn) as caught:
        conn.execute(text(insert), {"t": f"org-{uuid4()}", "o": uuid4()})
    assert sqlstate(caught) == "23503" and constraint_name(caught) == "job_sources_organization_id_fkey"


def test_a_role_run_and_a_brief_item_follow_their_role_and_introduction_out_of_existence(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    brief = brief_listing(conn, desk.cat, intro)
    with refused(conn) as caught:  # an item for an introduction that does not exist
        conn.execute(text(BRIEF_ITEM), {"b": brief, "p": 2, "u": desk.cat, "i": uuid4()})
    assert constraint_name(caught) == "brief_items_introduction_id_fkey"
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))  # the append-only ledger and consents
    conn.execute(text("DELETE FROM roles WHERE id = :r"), {"r": desk.role.id})
    assert count(conn, "brief_items WHERE brief_id = :b", b=brief) == 0
    assert count(conn, "match_runs WHERE role_id = :r", r=desk.role.id) == 0


def test_an_audit_row_outlives_its_organization(conn, desk):
    conn.execute(text(AUDIT_ROW), {"o": desk.org})
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))  # audit_log is append-only
    conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": desk.org})
    assert count(conn, "audit_log WHERE event_type = 'hiring.role_closed' AND organization_id IS NULL") == 1


# ---- erasure (invariant 7; DATA_MODEL 7.2) ------------------------------------------------------------------------


def deletion_request(conn: Connection, user: UUID) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
               VALUES (:u, :u, 'deletion', now() + interval '1 day') RETURNING id"""
        ),
        {"u": user},
    ).scalar_one()


def test_erasing_a_candidate_removes_their_asks_shortlist_entries_signals_and_brief_items(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    shortlist(conn, intro)
    signal(conn, "candidate", desk.cat, desk.org, desk.role.id, sent_by=desk.cat)
    signal(conn, "organization", desk.cat, desk.org, desk.role.id, intro=intro, sent_by=desk.owner)
    brief_listing(conn, desk.cat, intro)
    conn.execute(text("SELECT erase_user(:u, :r)"), {"u": desk.cat, "r": deletion_request(conn, desk.cat)})
    for table in ("introductions", "shortlist_entries", "interest_signals", "brief_items"):
        assert count(conn, f"{table} WHERE user_id = :u", u=desk.cat) == 0, table
    assert count(conn, "roles WHERE id = :r", r=desk.role.id) == 1  # the employer's role stays


def test_erasing_an_employer_who_answered_keeps_the_introduction_without_their_name(conn, desk):
    intro = ask(conn, desk.role, desk.cat)
    accept(conn, intro, desk.role, desk.cat)
    conn.execute(
        text(
            """UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = 'Strong fit',
                                        employer_answered_by = :o WHERE id = :i"""
        ),
        {"o": desk.owner, "i": intro},
    )
    conn.execute(text("SELECT erase_user(:u, :r)"), {"u": desk.owner, "r": deletion_request(conn, desk.owner)})
    row = intro_row(conn, intro)
    answered_by = names(conn, "SELECT employer_answered_by FROM introductions WHERE id = :i", i=intro)
    assert (row.answer, row.employer_answered_at is not None, answered_by) == ("yes", True, [None])
    assert count(conn, "org_members WHERE organization_id = :o", o=desk.org) == 0  # their membership went
    assert names(conn, "SELECT created_by FROM organizations WHERE id = :o", o=desk.org) == [None]
    assert names(conn, "SELECT created_by FROM roles WHERE id = :r", r=desk.role.id) == [None]


# ---- the enums, the functions' reach, and the revision itself ------------------------------------------------------


def data_model_labels(type_name: str) -> list[str]:
    source = (REPO / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")
    found = re.search(rf"CREATE TYPE\s+{type_name}\s+AS ENUM\s*\(([^)]*)\)", source)
    assert found, f"DATA_MODEL.md has no CREATE TYPE {type_name}"
    return re.findall(r"'([^']+)'", found.group(1))


@pytest.mark.parametrize("type_name", NEW_ENUMS)
def test_every_new_enum_holds_the_data_model_values_in_order(conn, type_name):
    labels = names(
        conn, "SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:t AS regtype) ORDER BY enumsortorder", t=type_name
    )
    assert labels == data_model_labels(type_name)


def test_the_eval_suites_include_the_role_intake_gold_set(conn):
    labels = names(conn, "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'eval_suite'::regtype")
    assert "role_intake" in labels and set(data_model_labels("eval_suite")) <= set(labels)


@pytest.mark.parametrize("function", ["my_org_invitations()", "introduction_role(uuid)"])
def test_the_two_functions_the_phase_added_run_for_the_api_and_the_worker_only(conn, function):
    can = {
        role: conn.execute(
            text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": role, "f": function}
        ).scalar_one()
        for role in ("app_api", "app_worker", "app_match")
    }
    assert can == {"app_api": True, "app_worker": True, "app_match": False}  # nothing on the hiring side for matching


def live_database_is_called_app(conn: Connection) -> None:
    """Make current_database() answer app for this transaction (see test_phase3_downgrades.py)."""
    conn.execute(text("CREATE SCHEMA live_stand_in"))
    conn.execute(
        text("CREATE FUNCTION live_stand_in.current_database() RETURNS name LANGUAGE sql AS $$ SELECT 'app'::name $$")
    )
    conn.execute(text("SET LOCAL search_path = live_stand_in, pg_catalog, public"))


def test_the_downgrade_refuses_to_run_on_the_live_database_before_it_changes_anything(conn):
    sql = (REPO / "db" / "sql" / "p6_0601_downgrade.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--")).strip()
    assert statements.startswith("DO $$"), "the guard must come before every DROP"
    live_database_is_called_app(conn)
    with refused(conn) as caught:
        conn.execute(text(sql))  # the whole downgrade: it must stop at its first statement
    assert message(caught) == "refusing a lossy downgrade on app"
    assert conn.execute(text("SELECT to_regclass('introductions')")).scalar_one() is not None


def test_the_revision_goes_down_one_step_leaving_nothing_and_comes_back(make_database, alembic_run):
    """The revision's own downgrade on a scratch database: nothing of it is left but the eval suite value (PostgreSQL
    cannot remove one), and the revision goes up again, twice, changing nothing the second time."""
    url = make_database(f"p6down_{os.getpid()}_test")
    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    try:
        alembic_run("upgrade", url, REVISION)
        alembic_run("downgrade", url, PREVIOUS)
        with engine.connect() as c:
            left = {
                "tables": names(c, "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES)),
                "types": names(c, "SELECT typname FROM pg_type WHERE typname = ANY(:n)", n=list(NEW_ENUMS)),
                "functions": names(c, "SELECT proname FROM pg_proc WHERE proname = ANY(:n)", n=list(NEW_FUNCTIONS)),
                "keys": names(c, "SELECT conname FROM pg_constraint WHERE conname = ANY(:n)", n=list(LATE_KEYS)),
            }
            assert left == {name: [] for name in left}
            assert "role_intake" in names(c, "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'eval_suite'::regtype")
        alembic_run("upgrade", url, REVISION)
        alembic_run("upgrade", url, REVISION)
        with engine.connect() as c:
            assert count(c, "pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES)) == len(NEW_TABLES)
            assert names(c, "SELECT version_num FROM alembic_version") == [REVISION]
    finally:
        engine.dispose()


def test_the_revision_sits_on_top_of_the_phase_4_head():
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PREVIOUS
    assert len(script.get_heads()) == 1
