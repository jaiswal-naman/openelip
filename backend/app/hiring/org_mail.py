"""The invitation email, template org_invitation_v1 (HIR-01; plan 06-03). Registering the resolver here is how the relay
learns the template; app.hiring.jobs imports this module, so the worker loads it (plan 06-01 wired that once).

The outbox row holds only the invitation's id. Who gets the message and what it says are worked out at send time, from
the invitation as it stands then: an invitation that was accepted, revoked or never existed is not sent. The text names
the organization and links to the page where the invitation is accepted; it names no inviter and no other member
(T-06-19).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import config, relay
from app.platform.relay import Outgoing, Refusal, is_undeliverable


def render(product_name: str, organization: str, link: str) -> tuple[str, str]:
    """(subject, body): plain text, one call to action."""
    subject = f"You are invited to join {organization} on {product_name}"
    body = (
        f"You have been invited to join {organization} on {product_name}.\n"
        "\n"
        "Sign in with this email address to accept the invitation:\n"
        f"{link}\n"
        "\n"
        "If you were not expecting this, you can ignore the email; nothing happens unless you accept.\n"
    )
    return subject, body


def resolve(conn: Connection, payload: dict[str, Any]) -> Outgoing | Refusal:
    """The relay's resolver for org_invitation_v1: the invited address and the message, or why it must not be sent."""
    try:
        member_id = UUID(str(payload["member_id"]))
    except (KeyError, TypeError, ValueError):
        return Refusal("failed", "bad_payload")
    invitation = conn.execute(
        text(
            """SELECT m.invited_email, o.name FROM org_members m JOIN organizations o ON o.id = m.organization_id
               WHERE m.id = :id AND m.status = 'invited'"""
        ),
        {"id": member_id},
    ).one_or_none()
    if invitation is None:
        return Refusal("cancelled", "stale")  # accepted, revoked or gone since it was queued
    if is_undeliverable(conn, invitation.invited_email):
        return Refusal("cancelled", "undeliverable")
    settings = config.get()
    subject, body = render(settings.product_name, invitation.name, f"{settings.app_base_url.rstrip('/')}/hire/roles")
    return Outgoing(invitation.invited_email, subject, body)


relay.RESOLVERS["org_invitation_v1"] = resolve
