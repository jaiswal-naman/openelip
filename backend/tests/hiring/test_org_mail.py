"""HIR-01: the invitation email, template org_invitation_v1 (plan 06-03).

The outbox row holds only an id; the resolver works out the address and the text when the relay sends. It runs here as
the worker role, the one the relay uses, over rows made with the 06-02 factories. Everything is invented.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.hiring import jobs as hiring_jobs  # noqa: F401  (importing it is how the worker loads the template)
from app.hiring import org_mail
from app.platform import config, relay
from app.platform.relay import Outgoing, Refusal
from tests.hiring import world
from tests.hiring.factories import make_candidate, make_member, make_org

ADDRESS = "new.colleague@staff.example.org"


def queue_invitation(conn, org, email=ADDRESS):
    member = make_member(conn, org, role="admin", invited=True, email=email)
    return member, {"member_id": str(member.id)}


def test_the_relay_knows_the_template(conn):
    assert relay.RESOLVERS["org_invitation_v1"] is org_mail.resolve


def test_an_open_invitation_is_a_plain_message_to_the_invited_address_naming_the_organization(conn):
    org = make_org(conn, "Halden Demo")
    _, payload = queue_invitation(conn, org)
    world.act_as(conn, "app_worker")

    message = org_mail.resolve(conn, payload)

    assert isinstance(message, Outgoing) and message.to == ADDRESS
    settings = config.get()
    assert "Halden Demo" in message.subject and settings.product_name in message.subject
    assert f"{settings.app_base_url.rstrip('/')}/hire/roles" in message.body
    assert "Halden Demo" in message.body


def test_the_message_names_no_inviter_and_no_other_member(conn):
    org = make_org(conn, "Halden Demo")
    colleague = make_member(conn, org, role="member")
    _, payload = queue_invitation(conn, org)
    owner_name = conn.execute(text("SELECT display_name FROM users WHERE id = :id"), {"id": org.owner}).scalar_one()
    owner_email = conn.execute(text("SELECT email FROM users WHERE id = :id"), {"id": org.owner}).scalar_one()
    world.act_as(conn, "app_worker")

    message = org_mail.resolve(conn, payload)

    everything = f"{message.subject}\n{message.body}"
    for private in (owner_name, owner_email, colleague.email, str(org.owner), str(colleague.user)):
        assert private not in everything


def test_an_invitation_that_was_accepted_is_not_sent(conn):
    org = make_org(conn)
    member, payload = queue_invitation(conn, org)
    person = make_candidate(conn, email=ADDRESS).id
    world.act_as(conn, "app_worker", person)
    conn.execute(text("SELECT accept_org_invitation(:id)"), {"id": member.id})

    assert org_mail.resolve(conn, payload) == Refusal("cancelled", "stale")


def test_an_invitation_that_was_revoked_or_never_existed_is_not_sent(conn):
    org = make_org(conn)
    member, payload = queue_invitation(conn, org)
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": member.id}
    )
    world.act_as(conn, "app_worker")

    assert org_mail.resolve(conn, payload) == Refusal("cancelled", "stale")
    assert org_mail.resolve(conn, {"member_id": str(uuid4())}) == Refusal("cancelled", "stale")


@pytest.mark.parametrize("payload", [{}, {"member_id": "not-a-uuid"}, {"member_id": None}])
def test_a_damaged_payload_is_failed_not_guessed(conn, payload):
    world.act_as(conn, "app_worker")

    assert org_mail.resolve(conn, payload) == Refusal("failed", "bad_payload")


def test_an_example_test_address_is_never_sent_to(conn):
    org = make_org(conn)
    _, payload = queue_invitation(conn, org, email="someone@mail.example.test")
    world.act_as(conn, "app_worker")

    assert org_mail.resolve(conn, payload) == Refusal("cancelled", "undeliverable")


def test_an_address_that_belongs_to_a_synthetic_account_is_never_sent_to(conn):
    org = make_org(conn)
    make_candidate(conn, email=ADDRESS, is_synthetic=True)
    _, payload = queue_invitation(conn, org)
    world.act_as(conn, "app_worker")

    assert org_mail.resolve(conn, payload) == Refusal("cancelled", "undeliverable")
