"""The small rules every hiring route shares (plan 06-01): who counts as a member, the hash of a preview, and how the
database's refusals read at the API. Every row is invented (tests/hiring/world.py)."""

import hashlib

import pytest
from sqlalchemy import Connection, text

from app.hiring import access, errors, share
from app.platform.errors import ApiError
from app.trust.api import ConsentRequired
from tests.hiring.world import (
    PREVIEW,
    accept,
    act_as,
    add_member,
    ask,
    candidate,
    exclude_company,
    insert_introduction,
    introduced,
    invite,
    make_org,
    open_role,
    overdue,
    refused,
)
from tests.match.factories import make_user

# ---- access: a member is an active member of the organization asked about ----------------------------------------


@pytest.fixture
def staff(conn: Connection):
    """An organization with an owner, an admin and a plain member, and the roles' people as a dict."""
    people = {role: make_user(conn) for role in ("owner", "admin", "member")}
    org = make_org(conn)
    for role, person in people.items():
        add_member(conn, org, person, role)
    return org, people


def request_as(conn: Connection, person) -> None:
    act_as(conn, "app_api", person)  # the API role, whose row-level security applies to what the helpers read


@pytest.mark.parametrize("role", ["owner", "admin", "member"])
def test_a_member_is_told_their_role(conn, staff, role):
    org, people = staff
    request_as(conn, people[role])
    assert access.require_member(conn, org) == role


@pytest.mark.parametrize("who", ["a stranger", "an invited person", "a removed member", "nobody signed in"])
def test_anyone_who_is_not_an_active_member_finds_no_such_organization(conn, staff, who):
    org, people = staff
    person = {
        "a stranger": lambda: make_user(conn),
        "an invited person": lambda: make_user(conn, email="invited@example.test"),
        "a removed member": lambda: people["member"],
        "nobody signed in": lambda: None,
    }[who]()
    if who == "an invited person":
        invite(conn, org, "invited@example.test", "admin")
    if who == "a removed member":
        conn.execute(
            text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE user_id = :u"), {"u": person}
        )
    request_as(conn, person)
    for check in (access.require_member, access.require_admin):
        with pytest.raises(ApiError) as caught:
            check(conn, org)
        assert (caught.value.status, caught.value.code) == (404, "not_found")  # never 403: it is not theirs to know


def test_a_member_of_one_organization_finds_another_one_missing(conn, staff):
    _, people = staff
    other = make_org(conn, "Birch Demo")  # made before the request starts: the API role cannot insert organizations
    request_as(conn, people["owner"])
    with pytest.raises(ApiError) as caught:
        access.require_member(conn, other)
    assert caught.value.status == 404


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owners_and_admins_pass_the_admin_check(conn, staff, role):
    org, people = staff
    request_as(conn, people[role])
    assert access.require_admin(conn, org) == role


def test_a_plain_member_is_visible_but_not_allowed(conn, staff):
    org, people = staff
    request_as(conn, people["member"])
    with pytest.raises(ApiError) as caught:
        access.require_admin(conn, org)
    assert (caught.value.status, caught.value.code) == (403, "forbidden")


# ---- share: the hash of what a candidate is shown ---------------------------------------------------------------


def test_a_preview_has_one_hash_whatever_the_order_of_its_keys():
    reordered = {
        "on_employer_yes": {"email": PREVIEW["on_employer_yes"]["email"]},
        "on_accept": dict(reversed(list(PREVIEW["on_accept"].items()))),
    }
    assert share.preview_hash(reordered) == share.preview_hash(PREVIEW)


def test_any_change_to_a_preview_changes_its_hash():
    seen = {share.preview_hash(PREVIEW)}
    for change in (
        {"on_accept": {**PREVIEW["on_accept"], "name": "Someone Else"}},
        {"on_employer_yes": {"email": "other@example.test"}},
        {"on_accept": {**PREVIEW["on_accept"], "facts": [{"kind": "role", "statement": "Added"}]}},
    ):
        seen.add(share.preview_hash({**PREVIEW, **change}))
    assert len(seen) == 4
    first, second = {"list": ["a", "b"]}, {"list": ["b", "a"]}
    assert share.preview_hash(first) != share.preview_hash(second)  # a list keeps its order: it is evidence in order


def test_the_hash_is_the_sha256_of_compact_sorted_unescaped_json():
    preview = {"on_accept": {"name": "Zoë Demo", "headline": None}, "a": 1}
    expected = hashlib.sha256('{"a":1,"on_accept":{"headline":null,"name":"Zoë Demo"}}'.encode()).hexdigest()
    assert share.preview_hash(preview) == expected  # non-ASCII names are hashed as they are written


# ---- errors: the database's refusals as the API catalogue words them -----------------------------------------------


def refusal(conn: Connection, which: str) -> BaseException:
    """The real exception the database raises for one of its refusals."""
    org = make_org(conn)
    role = open_role(conn, org)
    if which == "consent_missing":
        shy = candidate(conn, open_to_introductions=False)
        decision = introduced(conn, role, shy)
        act_as(conn, "app_worker")
        with refused(conn) as caught:
            insert_introduction(conn, role, shy, decision)
    elif which == "accepting without consent":
        person = candidate(conn)
        intro = ask(conn, role, person)
        with refused(conn) as caught:
            conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    elif which == "hard_constraint":
        person = candidate(conn)
        exclude_company(conn, person, "Halden Demo")
        decision = introduced(conn, role, person)
        with refused(conn) as caught:
            insert_introduction(conn, role, person, decision)
    elif which == "answer_clock":
        waiting = candidate(conn)
        owed = ask(conn, role, waiting)
        accept(conn, owed, role, waiting)
        overdue(conn, owed)
        person = candidate(conn)
        decision = introduced(conn, role, person)
        with refused(conn) as caught:
            insert_introduction(conn, role, person, decision)
    elif which == "expired":  # a check violation with another prefix
        person = candidate(conn)
        intro = ask(conn, role, person)
        with refused(conn) as caught:
            conn.execute(text("UPDATE introductions SET status = 'expired' WHERE id = :i"), {"i": intro})
    elif which == "unique":  # another kind of error altogether
        person = candidate(conn)
        ask(conn, role, person)
        decision = introduced(conn, role, person)
        with refused(conn) as caught:
            insert_introduction(conn, role, person, decision)
    else:  # append-only: a consent exists to be changed
        candidate(conn)
        with refused(conn) as caught:
            conn.execute(text("UPDATE consents SET text_version = 'changed'"))
    conn.execute(text("RESET ROLE"))
    return caught.value


def test_a_missing_consent_reads_as_403_consent_required_naming_which_consent(conn):
    error = errors.from_db(refusal(conn, "consent_missing"))
    assert isinstance(error, ConsentRequired) and (error.status, error.code) == (403, "consent_required")
    assert error.details["purpose"] == "open_to_introductions"
    after = errors.from_db(refusal(conn, "accepting without consent"))
    assert after is not None and after.status == 403 and after.details["purpose"] == "introduction"


def test_a_constraint_the_role_breaks_reads_as_422_violates_constraint(conn):
    error = errors.from_db(refusal(conn, "hard_constraint"))
    assert error is not None and (error.status, error.code) == (422, "violates_constraint")


def test_an_overdue_answer_reads_as_409_conflict_answers_overdue(conn):
    error = errors.from_db(refusal(conn, "answer_clock"))
    assert error is not None and (error.status, error.code) == (409, "conflict")
    assert error.details == {"state": "answers_overdue"}


@pytest.mark.parametrize("which", ["expired", "unique", "append-only"])
def test_any_other_refusal_is_left_to_the_caller(conn, which):
    assert errors.from_db(refusal(conn, which)) is None


def test_the_bare_database_exception_maps_too_and_a_foreign_exception_does_not(conn):
    caught = refusal(conn, "hard_constraint")
    assert errors.from_db(caught.orig).code == "violates_constraint"  # the driver's own exception, without the wrapper
    assert errors.from_db(ValueError("hard_constraint")) is None
