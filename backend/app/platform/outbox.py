"""The transactional outbox for email (ARCHITECTURE 5.5).

A message is inserted in the transaction that causes it, so the cause and the message commit together or not at all.
The relay (relay.py) sends it later. The recipient and body are worked out at send time, so a row holds ids; the one
exception is a sign-in code, which cannot be recovered from its HMAC and travels sealed until the relay blanks it.
"""

import json
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
) -> UUID | None:
    """Insert one message in the caller's transaction. None when the dedupe key already exists.

    `kind` is the template name (the `template` column) that the relay dispatches on, for example "signin".
    """
    row = conn.execute(
        text(
            """INSERT INTO outbox (dedupe_key, user_id, template, payload, not_before, expires_at)
               VALUES (:dedupe_key, :user_id, :kind, CAST(:payload AS jsonb), COALESCE(:not_before, now()), :expires_at)
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
        },
    ).one_or_none()
    new_id: UUID | None = None if row is None else row.id
    return new_id
