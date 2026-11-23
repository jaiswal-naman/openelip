"""HIR-01: /v1/orgs over HTTP (plan 06-03): create, get, invite, join, change a role, remove, claim ownership.

The API opens its own connections as the real app_api role, so row-level security and the grants are in force and what
a test commits stays: these tests use api_client, build rows with the 06-02 factories on a committing connection and
sign a person in by overriding the session dependency. The membership rules the database cannot state (last owner, who may
make an owner, claiming) are checked here against the real tables. Everything is invented.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.platform.sessions import Principal, current_user
from tests.hiring.factories import make_candidate, make_member, make_org

INVITEE = "new.colleague@staff.example.org"


@pytest.fixture
def desk(engine, api_client):
    d = SimpleNamespace(client=api_client, engine=engine)
    with engine.begin() as conn:
        d.org, d.other = make_org(conn), make_org(conn, "Brightwell Demo")
        d.admin = make_member(conn, d.org, role="admin")
        d.member = make_member(conn, d.org, role="member")
    d.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    d.sign_in(d.org.owner)
    return d


def key():
    return {"Idempotency-Key": str(uuid4())}


def ok(response, status=200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status, code, **details):
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    assert body["error"]["details"] == details
    return body["error"]


def invite(desk, email=INVITEE, role="member", org=None):
    return desk.client.post(
        f"/v1/orgs/{(org or desk.org).id}/members", json={"email": email, "role": role}, headers=key()
    )


def rows(desk, sql, **params):
    with desk.engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def membership(desk, member):
    return rows(desk, "SELECT role::text, status::text FROM org_members WHERE id = :id", id=member.id)[0]


# ---- create and get ------------------------------------------------------------------------------------------------


def test_creating_an_organization_makes_the_caller_its_owner(desk):
    with desk.engine.begin() as conn:
        person = make_candidate(conn).id
    desk.sign_in(person)

    org = ok(
        desk.client.post("/v1/orgs", json={"name": "Quill Works", "timezone": "Europe/Berlin"}, headers=key()), 201
    )

    assert (org["name"], org["timezone"], org["role"], org["is_synthetic"]) == (
        "Quill Works",
        "Europe/Berlin",
        "owner",
        False,
    )
    assert [(m["user_id"], m["role"], m["status"]) for m in org["members"]] == [(str(person), "owner", "active")]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Quill Works", "timezone": "Mars/Olympus"},
        {"name": "Quill Works", "timezone": "UTC+3"},
        {"name": "Quill Works", "timezone": "../etc/passwd"},
        {"name": "  ", "timezone": "UTC"},
        {"name": "", "timezone": "UTC"},
        {"timezone": "UTC"},
    ],
)
def test_an_unknown_zone_or_a_blank_name_is_validation_failed(desk, body):
    response = desk.client.post("/v1/orgs", json=body, headers=key())

    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


def test_repeating_a_create_with_the_same_key_makes_one_organization(desk):
    with desk.engine.begin() as conn:
        desk.sign_in(make_candidate(conn).id)
    headers, body = key(), {"name": "Quill Works", "timezone": "UTC"}

    first = desk.client.post("/v1/orgs", json=body, headers=headers)
    again = desk.client.post("/v1/orgs", json=body, headers=headers)

    assert (first.status_code, again.status_code) == (201, 201)
    assert rows(desk, "SELECT count(*) FROM organizations WHERE name = 'Quill Works'")[0][0] == 1


def test_get_shows_members_and_pending_invitations_and_a_stranger_gets_not_found(desk):
    ok(invite(desk), 201)

    org = ok(desk.client.get(f"/v1/orgs/{desk.org.id}"))

    assert org["timezone"] == "UTC" and org["is_synthetic"] is True and org["role"] == "owner"
    states = sorted((m["status"], m["role"], m["email"] is not None) for m in org["members"])
    assert states == [
        ("active", "admin", True),
        ("active", "member", True),
        ("active", "owner", True),
        ("invited", "member", True),
    ]
    desk.sign_in(desk.other.owner)
    refused(desk.client.get(f"/v1/orgs/{desk.org.id}"), 404, "not_found")


# ---- invite --------------------------------------------------------------------------------------------------------


def test_an_owner_or_admin_invites_and_one_email_is_queued(desk):
    invited = ok(invite(desk, role="admin"), 201)

    assert (invited["email"], invited["role"], invited["status"], invited["user_id"]) == (
        INVITEE,
        "admin",
        "invited",
        None,
    )
    outbox = rows(
        desk, "SELECT template, dedupe_key, user_id, payload FROM outbox WHERE template = 'org_invitation_v1'"
    )
    assert [(o.dedupe_key, o.user_id, o.payload) for o in outbox] == [
        (f"org_invitation:{invited['member_id']}", None, {"member_id": invited["member_id"]})
    ]
    audit = rows(desk, "SELECT organization_id, details FROM audit_log WHERE event_type = 'hiring.member_invited'")
    assert [(a.organization_id, set(a.details)) for a in audit] == [(desk.org.id, {"member_id", "role"})]
    desk.sign_in(desk.admin.user)
    assert ok(invite(desk, "second@staff.example.org"), 201)["status"] == "invited"
    assert len(rows(desk, "SELECT 1 FROM outbox WHERE template = 'org_invitation_v1'")) == 2


def test_the_same_request_twice_queues_one_email_and_a_second_try_is_a_conflict(desk):
    headers = key()
    body = {"email": INVITEE, "role": "member"}
    first = desk.client.post(f"/v1/orgs/{desk.org.id}/members", json=body, headers=headers)
    replay = desk.client.post(f"/v1/orgs/{desk.org.id}/members", json=body, headers=headers)
    assert (first.status_code, replay.status_code) == (201, 201)

    refused(invite(desk), 409, "conflict", state="invited")
    refused(invite(desk, INVITEE.upper()), 409, "conflict", state="invited")  # addresses compare without case

    assert len(rows(desk, "SELECT 1 FROM outbox WHERE template = 'org_invitation_v1'")) == 1
    assert len(rows(desk, "SELECT 1 FROM org_members WHERE status = 'invited'")) == 1


def test_inviting_an_active_member_is_a_conflict_in_state_member(desk):
    refused(invite(desk, desk.member.email), 409, "conflict", state="member")
    assert rows(desk, "SELECT 1 FROM outbox WHERE template = 'org_invitation_v1'") == []


def test_a_plain_member_cannot_invite_and_the_role_owner_is_not_offered(desk):
    desk.sign_in(desk.member.user)
    refused(invite(desk), 403, "forbidden")

    desk.sign_in(desk.org.owner)
    response = invite(desk, role="owner")
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"
    response = desk.client.post(f"/v1/orgs/{desk.org.id}/members", json={"email": "not-an-address"}, headers=key())
    assert response.status_code == 422


# ---- join ----------------------------------------------------------------------------------------------------------


def test_the_invited_person_joins_with_the_invited_address_and_nobody_else_can(desk):
    ok(invite(desk, role="admin"), 201)
    with desk.engine.begin() as conn:
        joiner = make_candidate(conn, email=INVITEE).id
        stranger = make_candidate(conn).id

    desk.sign_in(stranger)
    refused(desk.client.post(f"/v1/orgs/{desk.org.id}/join", headers=key()), 404, "not_found")

    desk.sign_in(joiner)
    org = ok(desk.client.post(f"/v1/orgs/{desk.org.id}/join", headers=key()))
    assert org["role"] == "admin"
    assert any(m["user_id"] == str(joiner) and m["status"] == "active" for m in org["members"])
    refused(desk.client.post(f"/v1/orgs/{desk.org.id}/join", headers=key()), 404, "not_found")
    assert ok(desk.client.get(f"/v1/orgs/{desk.org.id}"))["id"] == str(desk.org.id)


def test_an_invitation_to_another_organization_does_not_open_this_one(desk):
    desk.sign_in(desk.other.owner)
    ok(invite(desk, org=desk.other), 201)
    with desk.engine.begin() as conn:
        joiner = make_candidate(conn, email=INVITEE).id
    desk.sign_in(joiner)

    refused(desk.client.post(f"/v1/orgs/{desk.org.id}/join", headers=key()), 404, "not_found")
    refused(desk.client.get(f"/v1/orgs/{desk.org.id}"), 404, "not_found")  # an invitation is not a membership


# ---- change a role -------------------------------------------------------------------------------------------------


def patch(desk, member, role, org=None):
    return desk.client.patch(f"/v1/orgs/{(org or desk.org).id}/members/{member.id}", json={"role": role}, headers=key())


def test_an_admin_changes_member_and_admin_but_cannot_make_an_owner(desk):
    desk.sign_in(desk.admin.user)

    assert ok(patch(desk, desk.member, "admin"))["role"] == "admin"
    assert ok(patch(desk, desk.member, "member"))["role"] == "member"
    refused(patch(desk, desk.member, "owner"), 403, "forbidden")
    assert membership(desk, desk.member) == ("member", "active")


def test_an_owner_makes_an_owner_and_a_plain_member_changes_nothing(desk):
    assert ok(patch(desk, desk.admin, "owner"))["role"] == "owner"

    desk.sign_in(desk.member.user)
    refused(patch(desk, desk.admin, "member"), 403, "forbidden")
    assert membership(desk, desk.admin) == ("owner", "active")


def test_the_last_owner_cannot_be_demoted_but_one_of_two_can(desk):
    refused(patch(desk, owner_member(desk), "admin"), 409, "conflict", state="last_owner")

    ok(patch(desk, desk.admin, "owner"))
    assert ok(patch(desk, owner_member(desk), "member"))["role"] == "member"
    # one owner is left: the admin who was promoted cannot be demoted by themselves either
    desk.sign_in(desk.admin.user)
    refused(patch(desk, desk.admin, "admin"), 409, "conflict", state="last_owner")


def test_an_admin_cannot_demote_an_owner(desk):
    ok(patch(desk, desk.member, "owner"))  # two owners now
    desk.sign_in(desk.admin.user)

    refused(patch(desk, owner_member(desk), "member"), 403, "forbidden")


def test_an_invited_person_cannot_be_made_owner(desk):
    invited = ok(invite(desk), 201)

    refused(patch(desk, SimpleNamespace(id=invited["member_id"]), "owner"), 409, "conflict", state="invited")
    assert ok(patch(desk, SimpleNamespace(id=invited["member_id"]), "admin"))["role"] == "admin"


def owner_member(desk):
    row = rows(desk, "SELECT id FROM org_members WHERE organization_id = :o AND role = 'owner'", o=desk.org.id)[0]
    return SimpleNamespace(id=row.id)


# ---- remove --------------------------------------------------------------------------------------------------------


def remove(desk, member, org=None):
    return desk.client.delete(f"/v1/orgs/{(org or desk.org).id}/members/{member.id}", headers=key())


def test_removing_a_member_ends_their_access_and_writes_one_audit_row(desk):
    assert remove(desk, desk.member).status_code == 204

    row = rows(desk, "SELECT status::text, removed_at FROM org_members WHERE id = :id", id=desk.member.id)[0]
    assert row.status == "removed" and row.removed_at is not None
    desk.sign_in(desk.member.user)
    refused(desk.client.get(f"/v1/orgs/{desk.org.id}"), 404, "not_found")  # a removed member is a stranger
    audit = rows(desk, "SELECT organization_id, details FROM audit_log WHERE event_type = 'hiring.member_removed'")
    assert [(a.organization_id, a.details["member_id"]) for a in audit] == [(desk.org.id, str(desk.member.id))]


def test_revoking_an_invitation_stops_the_person_joining(desk):
    invited = ok(invite(desk), 201)
    with desk.engine.begin() as conn:
        joiner = make_candidate(conn, email=INVITEE).id

    assert remove(desk, SimpleNamespace(id=invited["member_id"])).status_code == 204

    desk.sign_in(joiner)
    refused(desk.client.post(f"/v1/orgs/{desk.org.id}/join", headers=key()), 404, "not_found")
    desk.sign_in(desk.org.owner)
    ok(invite(desk), 201)  # the same address can be invited again after a revoke


def test_the_last_owner_cannot_be_removed_and_an_admin_cannot_remove_an_owner(desk):
    refused(remove(desk, owner_member(desk)), 409, "conflict", state="last_owner")
    assert membership(desk, SimpleNamespace(id=owner_member(desk).id)) == ("owner", "active")

    ok(patch(desk, desk.member, "owner"))
    desk.sign_in(desk.admin.user)
    refused(remove(desk, owner_member(desk)), 403, "forbidden")

    desk.sign_in(desk.org.owner)
    assert remove(desk, desk.member).status_code == 204  # one of two owners may go


def test_a_plain_member_cannot_remove_anyone(desk):
    desk.sign_in(desk.member.user)

    refused(remove(desk, desk.admin), 403, "forbidden")


# ---- claim ownership -----------------------------------------------------------------------------------------------


def test_an_admin_claims_an_organization_with_no_owner_and_not_one_that_has_one(desk):
    desk.sign_in(desk.admin.user)
    refused(
        desk.client.post(f"/v1/orgs/{desk.org.id}/claim-ownership", headers=key()), 409, "conflict", state="has_owner"
    )

    with desk.engine.begin() as conn:  # the owner's account is erased: the membership row goes with it
        conn.execute(text("DELETE FROM org_members WHERE organization_id = :o AND role = 'owner'"), {"o": desk.org.id})
    desk.sign_in(desk.member.user)
    refused(desk.client.post(f"/v1/orgs/{desk.org.id}/claim-ownership", headers=key()), 403, "forbidden")

    desk.sign_in(desk.admin.user)
    org = ok(desk.client.post(f"/v1/orgs/{desk.org.id}/claim-ownership", headers=key()))
    assert org["role"] == "owner" and membership(desk, desk.admin) == ("owner", "active")
    refused(
        desk.client.post(f"/v1/orgs/{desk.org.id}/claim-ownership", headers=key()), 409, "conflict", state="has_owner"
    )


# ---- other organizations and signed out ----------------------------------------------------------------------------


def test_every_route_is_not_found_for_another_organization(desk):
    desk.sign_in(desk.other.owner)
    org = desk.org

    for response in (
        desk.client.get(f"/v1/orgs/{org.id}"),
        invite(desk, org=org),
        patch(desk, desk.member, "admin"),
        remove(desk, desk.member),
        desk.client.post(f"/v1/orgs/{org.id}/claim-ownership", headers=key()),
        desk.client.post(f"/v1/orgs/{org.id}/join", headers=key()),
    ):
        refused(response, 404, "not_found")
    assert membership(desk, desk.member) == ("member", "active")
    assert rows(desk, "SELECT 1 FROM outbox WHERE template = 'org_invitation_v1'") == []


def test_signed_out_is_unauthenticated_and_a_write_needs_a_key(desk):
    refused(desk.client.post("/v1/orgs", json={"name": "Q", "timezone": "UTC"}), 400, "idempotency_key_missing")
    desk.client.app.dependency_overrides.clear()

    assert desk.client.get(f"/v1/orgs/{desk.org.id}").status_code == 401
    assert desk.client.post("/v1/orgs", json={"name": "Q", "timezone": "UTC"}, headers=key()).status_code == 401
