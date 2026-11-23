"""HIR-04: the consent a candidate gives when they accept an ask belongs to one organization and one role, under the
wording the candidate was shown, and the audit writer can name the organization an event is about (plan 06-02).

The rows are the invented ones of tests/hiring/world.py, built on the `conn` fixture's rolled-back transaction.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform.errors import ApiError
from app.trust import api
from tests.hiring import world


def scene(conn):
    """An organization with two open roles and a candidate who is open to introductions."""
    org = world.make_org(conn)
    return org, world.open_role(conn, org), world.open_role(conn, org, title="Another role"), world.candidate(conn)


def is_consenting(conn, user, org, role) -> bool:
    return conn.execute(
        text("SELECT has_active_consent(:u, 'introduction', :o, :r)"), {"u": user, "o": org, "r": role}
    ).scalar_one()


def introduction_consents(conn, user):
    return conn.execute(
        text(
            """SELECT purpose::text, action::text, organization_id, role_id, text_version
               FROM consents WHERE user_id = :u AND purpose = 'introduction'"""
        ),
        {"u": user},
    ).all()


# ---- the grant ----------------------------------------


def test_the_wording_in_force_is_the_one_the_owner_will_approve():
    assert api.INTRODUCTION_TEXT_VERSION == "introduction-2026-11"


def test_a_grant_is_one_consent_row_for_one_organization_and_one_role(conn):
    org, role, other_role, user = scene(conn)
    grant_id = api.grant_introduction_consent(conn, user, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    assert [tuple(row) for row in introduction_consents(conn, user)] == [
        ("introduction", "granted", org, role.id, "introduction-2026-11")
    ]
    assert conn.execute(text("SELECT id FROM consents WHERE purpose = 'introduction'")).scalar_one() == grant_id
    assert is_consenting(conn, user, org, role.id) is True


def test_it_covers_that_role_only_not_another_role_of_the_same_organization_or_another_organization(conn):
    org, role, other_role, user = scene(conn)
    other_org = world.make_org(conn, "Brightwell Demo")
    api.grant_introduction_consent(conn, user, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    assert is_consenting(conn, user, org, other_role.id) is False
    assert is_consenting(conn, user, other_org, role.id) is False
    assert is_consenting(conn, world.candidate(conn), org, role.id) is False  # and not another person


@pytest.mark.parametrize("stale", ["introduction-2026-10", "", "introduction-2026-11 "])
def test_a_stale_wording_is_refused_with_the_current_version_and_stores_nothing(conn, stale):
    org, role, _, user = scene(conn)
    with pytest.raises(ApiError) as refused:
        api.grant_introduction_consent(conn, user, org, role.id, stale)
    error = refused.value
    assert (error.status, error.code) == (409, "conflict")
    assert error.details == {"state": "consent_text_changed", "current_version": "introduction-2026-11"}
    assert "introduction" in error.message.lower()
    assert introduction_consents(conn, user) == []


def test_the_general_grant_still_refuses_the_introduction_purpose(conn):
    user = world.candidate(conn)
    with pytest.raises(ValueError):
        api.grant_consent(conn, user, "introduction", api.INTRODUCTION_TEXT_VERSION)
    assert introduction_consents(conn, user) == []


def test_a_grant_leaves_no_audit_row_of_its_own_because_the_accept_writes_the_one_that_names_the_organization(conn):
    org, role, _, user = scene(conn)
    before = world.count(conn, "audit_log")
    api.grant_introduction_consent(conn, user, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    assert world.count(conn, "audit_log") == before


def test_the_candidate_records_it_as_themselves_and_cannot_record_one_for_another_person(conn):
    org, role, _, user = scene(conn)
    other = world.candidate(conn)
    world.act_as(conn, "app_api", user)
    api.grant_introduction_consent(conn, user, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    with world.refused(conn):
        api.grant_introduction_consent(conn, other, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    conn.execute(text("RESET ROLE"))
    assert is_consenting(conn, user, org, role.id) and not is_consenting(conn, other, org, role.id)


def test_the_accept_guard_asks_for_exactly_this_consent(conn):
    """introductions_guard lets an ask move to accepted only with a live consent for its organization and role."""
    org, role, _, user = scene(conn)
    intro = world.ask(conn, role, user)
    with world.refused(conn) as refused:
        conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    assert world.message(refused).startswith("consent_missing")
    api.grant_introduction_consent(conn, user, org, role.id, api.INTRODUCTION_TEXT_VERSION)
    conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    assert conn.execute(text("SELECT status::text FROM introductions WHERE id = :i"), {"i": intro}).scalar_one() == (
        "accepted"
    )


def test_a_consent_for_the_other_role_does_not_open_the_accept(conn):
    org, role, other_role, user = scene(conn)
    intro = world.ask(conn, role, user)
    api.grant_introduction_consent(conn, user, org, other_role.id, api.INTRODUCTION_TEXT_VERSION)
    with world.refused(conn) as refused:
        conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": intro})
    assert world.message(refused).startswith("consent_missing")


# ---- the organization on an audit row ----------------------------------------


def organizations_of_audit_rows(conn):
    return [row.organization_id for row in conn.execute(text("SELECT organization_id FROM audit_log ORDER BY id"))]


def test_an_audit_row_can_name_the_organization_it_is_about(conn):
    org, role, _, user = scene(conn)
    api.audit_log(
        conn,
        "hiring.role_published",
        "employer",
        None,
        {"role_id": role.id},
        actor_user_id=user,
        entity_table="roles",
        entity_id=str(role.id),
        organization_id=org,
    )
    (row,) = conn.execute(
        text("SELECT organization_id, actor_type::text, actor_user_id, entity_id, details FROM audit_log")
    ).all()
    assert (row.organization_id, row.actor_type, row.actor_user_id) == (org, "employer", user)
    assert (row.entity_id, row.details) == (str(role.id), {"role_id": str(role.id)})


def test_a_call_without_an_organization_stores_none_as_before(conn):
    api.audit_log(conn, "user.signed_in", "system", None)
    api.audit_log(conn, "consent.granted", "candidate", world.candidate(conn), {"purpose": "terms"})
    assert organizations_of_audit_rows(conn) == [None, None]


def test_the_organization_must_exist(conn):
    with world.refused(conn):
        api.audit_log(conn, "hiring.role_closed", "employer", None, organization_id=uuid4())
    assert organizations_of_audit_rows(conn) == []


def test_a_member_writes_a_row_about_their_organization_as_themselves_under_the_api_role(conn):
    org, _, _, user = scene(conn)
    world.add_member(conn, org, user)
    world.act_as(conn, "app_api", user)
    api.audit_log(conn, "hiring.role_closed", "employer", None, actor_user_id=user, organization_id=org)
    conn.execute(text("RESET ROLE"))
    assert organizations_of_audit_rows(conn) == [org]
