"""Revision p6_0601_hiring: row-level security on the hiring tables (HIR-01; plan 06-01, threats T-06-01 to T-06-06).

A member of one organization reads, changes and adds nothing of another organization's in any of the seven tables; an
employer sees an introduction only after the candidate accepted it; a candidate sees only their own; a removed member,
an invited person and an outside agent see nothing; the match role has no access to the hiring side at all. Each test
builds two employers, "Halden Demo" (a) and "Birch Demo" (b), each with a live role, a pending invitation, a candidate
who was asked and one who said yes (with a shortlist entry and signals), as the bootstrap login, then acts as one
person at a time with SET ROLE and app.user_id, as a request does. Every row is invented (tests/hiring/world.py).
"""

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from tests.hiring.world import (
    PREVIEW,
    Role,
    accept,
    act_as,
    add_member,
    ask,
    candidate,
    count,
    invite,
    make_org,
    open_role,
    refused,
    shortlist,
    signal,
    sqlstate,
)
from tests.match.factories import make_user

TABLES = (
    "organizations",
    "org_members",
    "roles",
    "role_requirements",
    "introductions",
    "shortlist_entries",
    "interest_signals",
)
ORG_KEY = {table: "id" if table == "organizations" else "organization_id" for table in TABLES}
# A statement that changes nothing when it matches: what matters is how many rows it reaches.
TOUCH = {
    "organizations": "name = name",
    "org_members": "role = role",
    "roles": "title = title",
    "role_requirements": "text = text",
    "introductions": "employer_answer_reason = employer_answer_reason",
    "shortlist_entries": "contact_released_at = contact_released_at",
    "interest_signals": "slot = slot",
}
POLICIES = {
    ("organizations", "member_read"),
    ("organizations", "admin_update"),
    ("org_members", "member_read"),
    ("org_members", "admin_write"),
    ("roles", "member_all"),
    ("role_requirements", "member_all"),
    ("introductions", "candidate_read"),
    ("introductions", "candidate_answer"),
    ("introductions", "employer_read"),
    ("introductions", "employer_answer"),
    ("shortlist_entries", "candidate_own"),
    ("shortlist_entries", "employer_read"),
    ("interest_signals", "candidate_read"),
    ("interest_signals", "candidate_send"),
    ("interest_signals", "employer_read"),
    ("interest_signals", "employer_send"),
} | {(table, "worker_all") for table in TABLES}
FULL = {"SELECT", "INSERT", "UPDATE", "DELETE"}


@dataclass(frozen=True)
class Employer:
    org: UUID
    owner: UUID
    member: UUID
    membership: UUID  # the plain member's org_members row
    invite: UUID  # a pending invitation's row
    role: Role
    asked: UUID  # a candidate who was asked and has not answered
    asked_intro: UUID
    yes: UUID  # a candidate who accepted
    yes_intro: UUID
    entry: UUID  # the shortlist entry of that acceptance


def employer(conn: Connection, name: str, tag: str) -> Employer:
    owner, member = make_user(conn), make_user(conn)
    org = make_org(conn, name, created_by=owner)
    add_member(conn, org, owner, "owner")
    membership = add_member(conn, org, member)
    pending = invite(conn, org, f"colleague-{tag}@example.test", "admin", owner)
    role = open_role(conn, org, name=name, created_by=owner)
    asked, yes = candidate(conn), candidate(conn)
    asked_intro = ask(conn, role, asked)
    yes_intro = ask(conn, role, yes)
    accept(conn, yes_intro, role, yes)
    entry = shortlist(conn, yes_intro)
    signal(conn, "candidate", asked, org, role.id, sent_by=asked)
    signal(conn, "candidate", yes, org, role.id, sent_by=yes)
    signal(conn, "organization", yes, org, role.id, intro=yes_intro, sent_by=owner)
    return Employer(org, owner, member, membership, pending, role, asked, asked_intro, yes, yes_intro, entry)


@pytest.fixture
def two(conn: Connection) -> tuple[Employer, Employer]:
    return employer(conn, "Halden Demo", "a"), employer(conn, "Birch Demo", "b")


def visible(conn: Connection, table: str, org: UUID) -> int:
    return count(conn, f"{table} WHERE {ORG_KEY[table]} = :o", o=org)


def ids(conn: Connection, sql: str, **params) -> set:
    return set(conn.execute(text(sql), params).scalars())


# ---- HIR-01: nothing crosses from one organization to another -----------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_a_member_reads_nothing_of_another_organization_and_all_of_their_own(conn, two, table):
    a, b = two
    assert visible(conn, table, a.org) > 0 and visible(conn, table, b.org) > 0  # the rows are there to be seen
    for person in (a.owner, a.member):
        act_as(conn, "app_api", person)
        assert visible(conn, table, b.org) == 0
        assert visible(conn, table, a.org) > 0


@pytest.mark.parametrize("table", TABLES)
def test_a_member_changes_nothing_of_another_organization(conn, two, table):
    a, b = two
    act_as(conn, "app_api", a.owner)
    update = f"UPDATE {table} SET {TOUCH[table]} WHERE {ORG_KEY[table]} = :o"
    assert conn.execute(text(update), {"o": b.org}).rowcount == 0
    if table != "introductions":  # the API role has no DELETE on introductions at all: see the grants below
        assert conn.execute(text(f"DELETE FROM {table} WHERE {ORG_KEY[table]} = :o"), {"o": b.org}).rowcount == 0


def other_organizations_inserts(b: Employer) -> dict[str, tuple[str, dict]]:
    week = "date_trunc('week', now() AT TIME ZONE 'UTC')::date"
    return {
        "organizations": ("INSERT INTO organizations (name) VALUES ('Spy Demo')", {}),
        "org_members": ("INSERT INTO org_members (organization_id, invited_email) VALUES (:o, 'spy@example.test')", {}),
        "roles": ("INSERT INTO roles (organization_id, title, description) VALUES (:o, 'Spy', 'Spy')", {}),
        "role_requirements": (
            """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text)
               VALUES (:r, :o, 9, 'required', 'skill', 'Spy')""",
            {},
        ),
        "introductions": (  # the ask is the worker's: the privilege check refuses it before anything else
            """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview)
               VALUES (:o, :r, :y, :d, '{}')""",
            {"d": uuid4()},
        ),
        "shortlist_entries": (  # a copy that would pass the trigger: only the policy is left to refuse it
            """INSERT INTO shortlist_entries (organization_id, role_id, introduction_id, user_id, shared_payload)
               VALUES (:o, :r, :i, :y, CAST(:p AS jsonb))""",
            {"p": json.dumps(PREVIEW)},
        ),
        "interest_signals": (  # an organization signal on an accepted introduction: the trigger passes, the policy not
            f"""INSERT INTO interest_signals (sender_side, user_id, organization_id, role_id, introduction_id, sent_by,
                                              week_start, slot)
                VALUES ('organization', :y, :o, :r, :i, :s, {week}, 3)""",
            {},
        ),
    }


@pytest.mark.parametrize("table", TABLES)
def test_a_member_cannot_add_a_row_to_another_organization(conn, two, table):
    a, b = two
    sql, extra = other_organizations_inserts(b)[table]
    act_as(conn, "app_api", a.owner)
    params = {"o": b.org, "r": b.role.id, "y": b.yes, "i": b.yes_intro, "s": a.owner, **extra}
    with refused(conn) as caught:
        conn.execute(text(sql), params)
    assert sqlstate(caught) == "42501"


def test_the_api_role_has_no_delete_on_introductions_and_no_insert_either(conn, two):
    a, _ = two
    act_as(conn, "app_api", a.yes)
    with refused(conn) as caught:
        conn.execute(text("DELETE FROM introductions WHERE id = :i"), {"i": a.yes_intro})
    assert sqlstate(caught) == "42501"


def test_a_removed_member_an_invited_person_and_an_outside_agent_see_nothing(conn, two):
    a, _ = two
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE id = :m"), {"m": a.membership}
    )
    act_as(conn, "app_api", a.member)  # removed: the same person who could read everything a moment ago
    assert [visible(conn, table, a.org) for table in TABLES] == [0] * len(TABLES)
    act_as(conn, "app_api", a.owner, token=uuid4())  # an owner's request, made by an outside agent
    assert [visible(conn, table, a.org) for table in TABLES] == [0] * len(TABLES)
    act_as(conn, "app_api", a.yes, token=uuid4())  # a candidate's request, made by an outside agent
    assert [count(conn, table) for table in TABLES] == [0] * len(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_the_match_role_has_no_access_to_the_hiring_side(conn, two, table):
    act_as(conn, "app_match")
    with refused(conn) as caught:
        count(conn, table)
    assert sqlstate(caught) == "42501"


def test_the_worker_serves_both_organizations(conn, two):
    a, b = two
    act_as(conn, "app_worker")
    for table in TABLES:
        assert visible(conn, table, a.org) > 0 and visible(conn, table, b.org) > 0, table


def test_the_policies_and_grants_are_the_data_models_and_nothing_more(conn):
    seen = {
        tuple(row)
        for row in conn.execute(
            text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:t)"), {"t": list(TABLES)}
        )
    }
    assert seen == POLICIES
    on = ids(conn, "SELECT relname FROM pg_class WHERE relname = ANY(:t) AND relrowsecurity", t=list(TABLES))
    assert on == set(TABLES)
    granted: dict[tuple[str, str], set[str]] = {}
    for grantee, table, privilege in conn.execute(
        text(
            """SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants
               WHERE table_name = ANY(:t) AND grantee IN ('app_api', 'app_worker', 'app_match', 'PUBLIC')"""
        ),
        {"t": list(TABLES)},
    ):
        granted.setdefault((grantee, table), set()).add(privilege)
    expected = {(role, table): FULL for role in ("app_api", "app_worker") for table in TABLES}
    expected[("app_api", "introductions")] = {"SELECT", "UPDATE"}  # the ask is the worker's; people only move it on
    expected[("app_worker", "introductions")] = {"SELECT", "INSERT", "UPDATE"}
    assert granted == expected


# ---- invariant 4: nothing about a candidate reaches an employer before the candidate acts ------------------------


def test_an_employer_sees_an_introduction_only_after_the_candidate_accepted_it(conn, two):
    a, _ = two
    for person in (a.owner, a.member):
        act_as(conn, "app_api", person)
        assert ids(conn, "SELECT id FROM introductions") == {a.yes_intro}  # the asked one is not there
        assert ids(conn, "SELECT user_id FROM shortlist_entries") == {a.yes}


def test_a_candidates_signal_stays_hidden_from_the_employer_until_the_candidate_accepts(conn, two):
    a, _ = two
    candidate_signals = "SELECT user_id FROM interest_signals WHERE sender_side = 'candidate'"
    act_as(conn, "app_api", a.owner)
    assert ids(conn, candidate_signals) == {a.yes}  # the one who accepted; not the one who was only asked
    conn.execute(text("RESET ROLE"))
    accept(conn, a.asked_intro, a.role, a.asked)
    act_as(conn, "app_api", a.owner)
    assert ids(conn, candidate_signals) == {a.yes, a.asked}


def test_the_shortlist_entry_disappears_for_the_employer_when_the_consent_is_withdrawn(conn, two):
    a, _ = two
    grant_id = conn.execute(
        text("SELECT id FROM consents WHERE user_id = :u AND purpose = 'introduction'"), {"u": a.yes}
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version)
               VALUES (:u, 'introduction', 'withdrawn', :g, 'test-v1')"""
        ),
        {"u": a.yes, "g": grant_id},
    )
    act_as(conn, "app_api", a.owner)
    assert count(conn, "shortlist_entries") == 0
    act_as(conn, "app_api", a.yes)
    assert ids(conn, "SELECT id FROM shortlist_entries") == {a.entry}  # their own copy stays theirs


def test_a_candidate_sees_only_their_own_introduction_entry_and_signals_and_nothing_of_employers(conn, two):
    a, _ = two
    act_as(conn, "app_api", a.yes)
    assert ids(conn, "SELECT id FROM introductions") == {a.yes_intro}
    assert ids(conn, "SELECT id FROM shortlist_entries") == {a.entry}
    assert count(conn, "interest_signals") == 2 and ids(conn, "SELECT user_id FROM interest_signals") == {a.yes}
    assert [count(conn, t) for t in ("organizations", "org_members", "roles", "role_requirements")] == [0] * 4
    act_as(conn, "app_api", a.asked)
    assert ids(conn, "SELECT id FROM introductions") == {a.asked_intro}
    assert count(conn, "shortlist_entries") == 0 and ids(conn, "SELECT user_id FROM interest_signals") == {a.asked}


# ---- who may move an introduction on ------------------------------------------------------------------------------

DECLINE = "UPDATE introductions SET status = 'declined' WHERE id = :i"
ANSWER = """UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = 'Strong fit',
                                 employer_answered_by = :u WHERE id = :i"""


def test_only_the_candidate_declines_their_own_ask(conn, two):
    a, b = two
    act_as(conn, "app_api", b.asked)  # another candidate
    assert conn.execute(text(DECLINE), {"i": a.asked_intro}).rowcount == 0
    act_as(conn, "app_api", a.owner)  # the employer cannot even see it
    assert conn.execute(text(DECLINE), {"i": a.asked_intro}).rowcount == 0
    act_as(conn, "app_api", a.yes)  # and an accepted ask is no longer the candidate's to move
    assert conn.execute(text(DECLINE), {"i": a.yes_intro}).rowcount == 0
    act_as(conn, "app_api", a.asked)
    assert conn.execute(text(DECLINE), {"i": a.asked_intro}).rowcount == 1


def test_a_member_answers_an_accepted_introduction_of_their_own_organization_once_and_as_themselves(conn, two):
    a, b = two
    act_as(conn, "app_api", a.owner)
    with refused(conn) as caught:  # the answer names the person who gave it
        conn.execute(text(ANSWER), {"u": a.member, "i": a.yes_intro})
    assert sqlstate(caught) == "42501"
    assert conn.execute(text(ANSWER), {"u": a.owner, "i": a.asked_intro}).rowcount == 0  # not accepted: invisible
    assert conn.execute(text(ANSWER), {"u": a.owner, "i": b.yes_intro}).rowcount == 0  # another organization's
    assert conn.execute(text(ANSWER), {"u": a.owner, "i": a.yes_intro}).rowcount == 1
    assert conn.execute(text(ANSWER), {"u": a.owner, "i": a.yes_intro}).rowcount == 0  # once


def test_signals_are_sent_as_oneself_a_candidate_for_any_role_and_a_member_for_their_own_organization(conn, two):
    a, b = two
    third = candidate(conn)
    third_intro = ask(conn, a.role, third)
    accept(conn, third_intro, a.role, third)
    week = "date_trunc('week', now() AT TIME ZONE 'UTC')::date"
    insert = f"""INSERT INTO interest_signals (sender_side, user_id, organization_id, role_id, introduction_id, sent_by,
                                               week_start, slot)
                 VALUES (CAST(:side AS signal_side), :u, :o, :r, :i, :s, {week}, 2)"""
    organization = {"side": "organization", "u": third, "o": a.org, "r": a.role.id, "i": third_intro}
    act_as(conn, "app_api", a.member)
    with refused(conn) as caught:  # on a colleague's behalf
        conn.execute(text(insert), {**organization, "s": a.owner})
    assert sqlstate(caught) == "42501"
    conn.execute(text(insert), {**organization, "s": a.member})

    mine = {"side": "candidate", "u": a.yes, "o": b.org, "r": b.role.id, "i": None, "s": a.yes}
    act_as(conn, "app_api", a.yes)
    conn.execute(text(insert), mine)  # a candidate may signal on any role: no row of the employer is read
    with refused(conn) as caught:  # but only as themselves
        conn.execute(text(insert), {**mine, "u": a.asked, "r": a.role.id})
    assert sqlstate(caught) == "42501"
    with refused(conn) as caught:  # and a candidate is no organization
        conn.execute(text(insert), {**organization, "s": a.yes})
    assert sqlstate(caught) == "42501"


def test_the_policies_let_each_member_do_what_the_data_model_says(conn, two):
    a, _ = two
    rename = "UPDATE organizations SET name = 'Halden Renamed Demo' WHERE id = :o"
    retitle = "UPDATE roles SET title = 'Senior data engineer' WHERE id = :r"
    act_as(conn, "app_api", a.member)
    assert conn.execute(text(rename), {"o": a.org}).rowcount == 0  # only an owner or admin renames
    assert conn.execute(text(retitle), {"r": a.role.id}).rowcount == 1  # any member edits roles
    act_as(conn, "app_api", a.owner)
    assert conn.execute(text(rename), {"o": a.org}).rowcount == 1
    with refused(conn) as caught:  # nobody makes an organization except through create_organization
        conn.execute(text("INSERT INTO organizations (name) VALUES ('Direct Demo')"))
    assert sqlstate(caught) == "42501"
    act_as(conn, "app_api", a.member)  # a plain member cannot invite or remove
    assert conn.execute(text("UPDATE org_members SET role = 'owner' WHERE id = :m"), {"m": a.membership}).rowcount == 0
    with refused(conn) as caught:
        conn.execute(
            text("INSERT INTO org_members (organization_id, invited_email) VALUES (:o, 'late@example.test')"),
            {"o": a.org},
        )
    assert sqlstate(caught) == "42501"


# ---- the entry points that run before a membership exists, or that must not open a whole table -------------------


def test_a_person_creates_an_organization_and_is_its_owner(conn):
    person = make_user(conn)
    act_as(conn, "app_api", person)
    org = conn.execute(text("SELECT create_organization('New Demo', 'Europe/Paris')")).scalar_one()
    row = conn.execute(
        text(
            """SELECT role::text, status::text, joined_at IS NOT NULL, user_id FROM org_members
               WHERE organization_id = :o"""
        ),
        {"o": org},
    ).one()
    assert tuple(row) == ("owner", "active", True, person)
    made = conn.execute(
        text("SELECT name, timezone, created_by, is_synthetic FROM organizations WHERE id = :o"), {"o": org}
    )
    assert tuple(made.one()) == ("New Demo", "Europe/Paris", person, False)


@pytest.mark.parametrize("who", ["nobody", "an outside agent"])
def test_an_organization_is_made_only_by_a_signed_in_person(conn, who):
    person = make_user(conn)
    act_as(conn, "app_api", None if who == "nobody" else person, token=None if who == "nobody" else uuid4())
    with refused(conn) as caught:
        conn.execute(text("SELECT create_organization('Ghost Demo', 'UTC')"))
    assert sqlstate(caught) == "42501"


def test_an_invitation_is_accepted_only_by_the_account_with_that_address(conn, two):
    a, _ = two
    stranger = make_user(conn)
    invited = make_user(conn, email="COLLEAGUE-A@example.test")  # another letter case: still the same address
    accept_sql = "SELECT accept_org_invitation(:m)"
    act_as(conn, "app_api", stranger)
    with refused(conn) as caught:
        conn.execute(text(accept_sql), {"m": a.invite})
    assert (
        sqlstate(caught) == "42501" and caught.value.orig.diag.message_primary == "no open invitation for this account"
    )
    act_as(conn, "app_api", invited)
    conn.execute(text(accept_sql), {"m": a.invite})
    row = conn.execute(
        text("SELECT user_id, status::text, role::text, joined_at IS NOT NULL FROM org_members WHERE id = :m"),
        {"m": a.invite},
    ).one()
    assert tuple(row) == (invited, "active", "admin", True)
    assert visible(conn, "roles", a.org) == 1  # now a member: the role is theirs to read
    with refused(conn):  # the invitation is used up
        conn.execute(text(accept_sql), {"m": a.invite})


def test_my_org_invitations_lists_the_open_invitations_of_the_callers_address_only(conn, two):
    a, _ = two
    me = make_user(conn, email="Colleague-A@example.test")
    stranger = make_user(conn)
    listing = "SELECT member_id, organization_id, organization_name, role::text, created_at FROM my_org_invitations()"
    act_as(conn, "app_api", me)
    (row,) = conn.execute(text(listing)).all()
    assert tuple(row[:4]) == (a.invite, a.org, "Halden Demo", "admin")
    for who, token in ((stranger, None), (None, None), (me, uuid4())):  # another address, nobody, an outside agent
        act_as(conn, "app_api", who, token=token)
        assert conn.execute(text(listing)).all() == []
    act_as(conn, "app_api", me)
    conn.execute(text("SELECT accept_org_invitation(:m)"), {"m": a.invite})
    assert conn.execute(text(listing)).all() == []  # joined: no longer pending


def test_the_directory_lists_my_colleagues_and_nobody_elses(conn, two):
    a, b = two
    act_as(conn, "app_api", a.owner)
    rows = conn.execute(text("SELECT email, role::text, status::text FROM org_directory(:o)"), {"o": a.org}).all()
    assert {(r.role, r.status) for r in rows} == {("owner", "active"), ("member", "active"), ("admin", "invited")}
    assert "colleague-a@example.test" in {r.email for r in rows}  # an invitation shows the address it went to
    assert conn.execute(text("SELECT count(*) FROM org_directory(:o)"), {"o": b.org}).scalar_one() == 0
    act_as(conn, "app_api", a.yes)  # a candidate is nobody's colleague
    assert conn.execute(text("SELECT count(*) FROM org_directory(:o)"), {"o": a.org}).scalar_one() == 0


def test_the_preview_is_for_members_of_the_roles_organization_only(conn, two):
    a, b = two
    preview = "SELECT role_preview(:r)"
    act_as(conn, "app_api", a.owner)
    assert conn.execute(text(preview), {"r": a.role.id}).scalar_one() is not None
    assert conn.execute(text(preview), {"r": b.role.id}).scalar_one() is None  # another organization's role
    act_as(conn, "app_api", a.yes)  # a candidate learns nothing about a role through it
    assert conn.execute(text(preview), {"r": a.role.id}).scalar_one() is None


def test_introduction_role_shows_a_candidate_the_role_they_were_asked_about_and_nobody_else(conn, two):
    a, _ = two
    conn.execute(
        text("UPDATE roles SET country = 'IN', city = 'Pune', work_arrangements = '{hybrid,remote}' WHERE id = :r"),
        {"r": a.role.id},
    )
    read = """SELECT organization_name, role_title, job_id, country, city,
                     CAST(work_arrangements AS text[]) AS arrangements, salary_min, salary_max, salary_currency,
                     is_synthetic FROM introduction_role(:i)"""
    act_as(conn, "app_api", a.asked)
    row = conn.execute(text(read), {"i": a.asked_intro}).one()
    assert tuple(row) == (
        "Halden Demo",
        "Data engineer",
        a.role.job.id,
        "IN",
        "Pune",
        ["hybrid", "remote"],
        80000,
        120000,
        "USD",
        False,
    )
    assert conn.execute(text(read), {"i": a.yes_intro}).all() == []  # another candidate's ask
    for who, token in ((a.owner, None), (None, None), (a.asked, uuid4())):  # the employer, nobody, an outside agent
        act_as(conn, "app_api", who, token=token)
        assert conn.execute(text(read), {"i": a.asked_intro}).all() == []
