"""The audit writer (DATA_MODEL 3.3 audit_log; TRU-03): one append-only row per automated decision or privileged access.

A row says what happened, to whom, and by whom, with ids, counts and codes in `details` and never text a person wrote
(PLT-07): a detail is a short string, a number, a flag or nothing, and a string over 200 characters is refused because
that length is prose, not a code. Everything is checked before the insert, so a mistake raises at the call and does not
spoil the caller's transaction.

Under the API role the table's insert policy requires actor_user_id to be the signed-in person, so a candidate's own
action carries them as the actor. A system event has no actor, and only the worker role may write one.
"""

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, insert

from . import models

EVENT_TYPE = re.compile(r"[a-z_]+\.[a-z_]+")  # 'module.what_happened', the table's own CHECK
MAX_DETAIL_LENGTH = 200


def _clean(details: Mapping[str, Any]) -> dict[str, Any]:
    """The details as stored: ids as strings; anything that is not an id, a count, a flag or a short code is refused."""
    clean: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, UUID):
            value = str(value)
        if value is not None and not isinstance(value, str | int):  # bool is an int
            raise ValueError(f"details[{key!r}] is not an id, a count, a flag or a code")
        if isinstance(value, str) and len(value) > MAX_DETAIL_LENGTH:
            raise ValueError(f"details[{key!r}] is over {MAX_DETAIL_LENGTH} characters: details hold codes, not text")
        clean[key] = value
    return clean


def audit_log(
    conn: Connection,
    event_type: str,
    actor_type: str,
    subject_user_id: UUID | None,
    details: Mapping[str, Any] | None = None,
    *,
    actor_user_id: UUID | None = None,
    entity_table: str | None = None,
    entity_id: str | None = None,
) -> None:
    """Append one row in the caller's transaction.

    subject_user_id is the person the event is about (None for an event about no one). For a candidate acting on their
    own record the actor is the subject, unless actor_user_id says otherwise. entity_table and entity_id point at the
    row the event is about, for example the consent that was granted.
    """
    if not EVENT_TYPE.fullmatch(event_type):
        raise ValueError("event_type must look like 'module.what_happened': lower-case letters and underscores")
    if actor_type not in models.ACTOR_TYPES:
        raise ValueError("actor_type is not one of the actor_type enum values")
    clean = _clean(details or {})
    if actor_type == "candidate" and actor_user_id is None:
        actor_user_id = subject_user_id
    conn.execute(
        insert(models.audit_log).values(
            event_type=event_type,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            subject_user_id=subject_user_id,
            entity_table=entity_table,
            entity_id=entity_id,
            details=clean,
        )
    )
