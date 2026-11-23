"""The follow-up's way out: the relay resolver for `follow_up_v1` (ARCHITECTURE 5.5, 5.6; INDEX decision 11).

The outbox row carries only the action id. When the relay claims it (after `undo_until`, never while the campaign is
paused) this resolver reads the action again, runs both keys again with new check rows, and either hands the relay the
message (to the address the candidate typed, `Reply-To` the candidate) or blocks the action and refuses the row, so
nothing is sent. Importing this module registers the template; the worker imports it through app.guardian.jobs.
"""

from typing import Any

from sqlalchemy import Connection

from app.platform import relay
from app.platform.relay import Outgoing, Refusal

from . import approvals

TEMPLATE = approvals.FOLLOW_UP_TEMPLATE


def resolve_follow_up(conn: Connection, payload: dict[str, Any]) -> Outgoing | Refusal:
    """follow_up_v1: the stored subject and body to the stored address, after the send-time recheck."""
    try:
        action_id = int(payload["agent_action_id"])
    except (KeyError, TypeError, ValueError):
        return Refusal("failed", "bad_payload")
    reason, action = approvals.recheck(conn, action_id)
    if reason is not None or action is None:
        return Refusal("cancelled", reason or "action_not_scheduled")
    message = action["payload"] or {}
    to = str(message.get("to") or "")
    if relay.is_undeliverable(conn, to):
        return Refusal("cancelled", "undeliverable")
    return Outgoing(to, str(message["subject"]), str(message["body"]), {"Reply-To": str(message["reply_to"])})


relay.RESOLVERS[TEMPLATE] = resolve_follow_up
