"""my_organizations and the memberships on GET /v1/me (plan 06-01, HIR-01).

Rows are invented (tests/hiring/world.py)."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from app.hiring import api as hiring
from app.hiring.queries import Invitation, Membership
from app.platform import auth, sessions
from app.platform.sessions import Principal, current_user
from tests.hiring.world import act_as, add_member, invite, make_org
from tests.match.factories import make_user

ANN = "ann@example.test"


@dataclass(frozen=True)
class AnnsWorld:
    person: UUID
    halden: UUID  # owned by her
    birch: UUID  # she is an admin there
    elm: UUID  # she is invited, by her address in another letter case
    elm_invitation: UUID


def build_world(conn: Connection) -> AnnsWorld:
    """Ann, with two memberships and one invitation, and everything around her that must not show: an organization she
    was removed from, an invitation to someone else, and another person's membership."""
    ann = make_user(conn, email=ANN)
    halden = make_org(conn, "Halden Demo", created_by=ann)
    add_member(conn, halden, ann, "owner")
    birch = make_org(conn, "Birch Demo")
    add_member(conn, birch, ann, "admin")
    cedar = make_org(conn, "Cedar Demo")
    add_member(conn, cedar, ann, "member")
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE organization_id = :o"), {"o": cedar}
    )
    elm = make_org(conn, "Elm Demo")
    elm_invitation = invite(conn, elm, "ANN@Example.test", "admin")
    invite(conn, make_org(conn, "Fir Demo"), "someone.else@example.test")
    add_member(conn, make_org(conn, "Gum Demo"), make_user(conn))
    return AnnsWorld(ann, halden, birch, elm, elm_invitation)


@pytest.mark.parametrize("rls", [True, False], ids=["under row-level security", "on its own filters"])
def test_my_organizations_lists_the_active_memberships_and_the_open_invitations_of_the_person(conn, rls):
    """Run both ways: row-level security alone would hide a removed membership, so without it the query's own
    `status = 'active'` is what keeps Cedar Demo out of the list."""
    world = build_world(conn)
    conn.execute(text("UPDATE organizations SET is_synthetic = true WHERE id = :o"), {"o": world.halden})
    if rls:
        act_as(conn, "app_api", world.person)
    else:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(world.person)})
    memberships, invitations = hiring.my_organizations(conn, world.person)
    assert memberships == [
        Membership(world.birch, "Birch Demo", "admin", False),
        Membership(world.halden, "Halden Demo", "owner", True),  # a demo organization says so
    ]
    (invitation,) = invitations
    assert isinstance(invitation, Invitation)
    assert (invitation.member_id, invitation.organization_id, invitation.organization_name, invitation.role) == (
        world.elm_invitation,
        world.elm,
        "Elm Demo",
        "admin",
    )
    assert invitation.invited_at == conn.execute(text("SELECT now()")).scalar_one()


def test_a_person_with_no_organization_has_two_empty_lists(conn):
    build_world(conn)
    stranger = make_user(conn)
    act_as(conn, "app_api", stranger)
    assert hiring.my_organizations(conn, stranger) == ([], [])


def test_an_outside_agent_is_listed_nothing(conn):
    world = build_world(conn)
    act_as(conn, "app_api", world.person, token=uuid4())
    memberships, invitations = hiring.my_organizations(conn, world.person)
    assert (memberships, invitations) == ([], [])


# ---- GET /v1/me and the sign-in response ------------------------------------------------------------------------


@pytest.fixture
def ann(engine, api_client) -> AnnsWorld:
    """Ann as the API sees her: committed rows, signed in (the session dependency is overridden)."""
    with engine.begin() as c:
        world = build_world(c)
    api_client.app.dependency_overrides[current_user] = lambda: Principal(world.person)
    return world


def test_me_lists_the_organizations_and_the_invitations(api_client, ann):
    me = api_client.get("/v1/me").json()["data"]
    assert [(o["id"], o["name"], o["role"], o["is_synthetic"]) for o in me["organizations"]] == [
        (str(ann.birch), "Birch Demo", "admin", False),
        (str(ann.halden), "Halden Demo", "owner", False),
    ]
    (invitation,) = me["invitations"]
    assert set(invitation) == {
        "organization_id",
        "organization_name",
        "role",
        "invited_at",
    }  # joining is by organization
    assert (invitation["organization_id"], invitation["organization_name"], invitation["role"]) == (
        str(ann.elm),
        "Elm Demo",
        "admin",
    )
    datetime.fromisoformat(invitation["invited_at"])  # a time, as ISO text


def test_a_plain_candidate_has_empty_lists_on_me(api_client, engine):
    with engine.begin() as c:
        person = make_user(c)
    api_client.app.dependency_overrides[current_user] = lambda: Principal(person)
    me = api_client.get("/v1/me").json()["data"]
    assert (me["organizations"], me["invitations"]) == ([], [])


def test_signing_in_reports_the_same_organizations(api_client, engine, ann):
    api_client.app.dependency_overrides.clear()  # a real sign-in this time, with the emailed code
    auth._recent.clear()  # the per-address limit lives in this process and every test client shares it
    assert api_client.post("/v1/auth/codes", json={"email": ANN}).status_code == 202
    with engine.connect() as c:
        sealed = c.execute(
            text(
                """SELECT o.payload->>'code_sealed' FROM outbox o
                   JOIN login_codes l ON l.id = CAST(o.payload->>'login_code_id' AS uuid)
                   WHERE lower(l.email) = :e ORDER BY o.created_at DESC LIMIT 1"""
            ),
            {"e": ANN},
        ).scalar_one()
    signed = api_client.post("/v1/auth/sessions", json={"email": ANN, "code": sessions.open_sealed(sealed)})
    assert signed.status_code == 201
    me = signed.json()["data"]
    assert [o["name"] for o in me["organizations"]] == ["Birch Demo", "Halden Demo"]
    assert [i["organization_name"] for i in me["invitations"]] == ["Elm Demo"]
