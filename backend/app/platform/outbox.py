"""The transactional outbox for email (ARCHITECTURE 5.5).

A message is inserted in the transaction that causes it, so the cause and the message commit together or not at all.
The relay (relay.py) sends it later. The recipient and body are worked out at send time, so a row holds ids; the one
exception is a sign-in code, which cannot be recovered from its HMAC and travels sealed until the relay blanks it.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def add(
    conn: Connection,
    *,
    kind: str,
    dedupe_key: str,
    user_id: UUID | None,
    payload: dict[str, Any],
    not_before: datetime | None = None,
    expires_at: datetime | None = None,
    agent_action_id: int | None = None,
) -> UUID | None:
    """Insert one message in the caller's transaction. None when the dedupe key already exists.

    `kind` is the template name (the `template` column) that the relay dispatches on, for example "signin".
    `agent_action_id` marks the message as an agent's outside action: the database then accepts it only for a
    scheduled action and not before the action's undo window ends (DATA_MODEL 4.6), and only one row per action.
    """
    row = conn.execute(
        text(
            """INSERT INTO outbox (dedupe_key, user_id, template, payload, not_before, expires_at, agent_action_id)
               VALUES (:dedupe_key, :user_id, :kind, CAST(:payload AS jsonb), COALESCE(:not_before, now()), :expires_at,
                       :agent_action_id)
               ON CONFLICT (dedupe_key) DO NOTHING
               RETURNING id"""
        ),
        {
            "dedupe_key": dedupe_key,
            "user_id": user_id,
            "kind": kind,
            "payload": json.dumps(payload),
            "not_before": not_before,
            "expires_at": expires_at,
            "agent_action_id": agent_action_id,
        },
    ).one_or_none()
    new_id: UUID | None = None if row is None else row.id
    return new_id


def action_states(conn: Connection, action_ids: Sequence[int]) -> dict[int, str]:
    """The outbox status (pending, sent, ...) of each action that has a message; an action with none is left out."""
    if not action_ids:
        return {}
    rows = conn.execute(
        text(
            """SELECT agent_action_id, status::text AS status FROM outbox
               WHERE agent_action_id = ANY(CAST(:ids AS bigint[]))"""
        ),
        {"ids": list(action_ids)},
    )
    return {int(row.agent_action_id): row.status for row in rows}


def hold(conn: Connection, action_ids: Sequence[int]) -> int:
    """Keep the pending messages of these actions from being sent: not_before becomes infinity. Returns how many."""
    if not action_ids:
        return 0
    return conn.execute(
        text(
            """UPDATE outbox SET not_before = 'infinity'
               WHERE agent_action_id = ANY(CAST(:ids AS bigint[])) AND status = 'pending'"""
        ),
        {"ids": list(action_ids)},
    ).rowcount


def release(conn: Connection, action_ids: Sequence[int], not_before: datetime) -> int:
    """Let the held pending messages of these actions go at `not_before`. Returns how many."""
    if not action_ids:
        return 0
    return conn.execute(
        text(
            """UPDATE outbox SET not_before = :not_before
               WHERE agent_action_id = ANY(CAST(:ids AS bigint[])) AND status = 'pending'"""
        ),
        {"ids": list(action_ids), "not_before": not_before},
    ).rowcount


def cancel_for_actions(conn: Connection, action_ids: Sequence[int], code: str = "undone") -> list[UUID]:
    """Cancel the pending messages of these actions (a message that started sending is left alone) and return their ids.
    `code` is stored as the last error code, `undone` unless the caller says why else."""
    if not action_ids:
        return []
    rows = conn.execute(
        text(
            """UPDATE outbox SET status = 'cancelled', last_error_code = :code
               WHERE agent_action_id = ANY(CAST(:ids AS bigint[])) AND status = 'pending'
               RETURNING id"""
        ),
        {"ids": list(action_ids), "code": code},
    )
    return [row.id for row in rows]
