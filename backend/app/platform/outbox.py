"""The transactional outbox for email (plan 01-03 implements it; ARCHITECTURE 5.5)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection


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
    """Insert one message in the caller's transaction. None when the dedupe key already exists."""
    raise NotImplementedError("plan 01-03")
