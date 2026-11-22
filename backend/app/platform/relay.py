"""The relay that sends outbox rows by SMTP (plan 01-03 implements it; ARCHITECTURE 5.5)."""

from uuid import UUID

from sqlalchemy import Connection


def relay_once(conn: Connection) -> int:
    """Send the outbox rows that are due and return how many. Until plan 01-03 nothing is sent."""
    return 0


def resolve(outbox_id: UUID, sent: bool) -> None:
    """The operator's answer for a row left in `unknown`: it was sent, or it was not (`outbox resolve`)."""
    raise NotImplementedError("plan 01-03")
