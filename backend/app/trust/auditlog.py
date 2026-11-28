"""The candidate's audit log of automated decisions (TRU-03, API 2.20): one merged, paged reader over sources.

`trust` is layer 1 and cannot import the modules that own the decisions (match runs, outside actions, Twin messages), so
each of them registers a reader at start-up with `source(name, reader)`, and `read` merges what the readers return. A
reader runs on the caller's own connection under row-level security and filters on the caller's id as well, so a person
sees only their own entries. Nothing is stored twice and nothing is written back: older history appears too, because the
readers read the tables that already exist.

An entry's text is built by code from codes and counts. No question, reply, message or draft text enters it.
Entries sort newest first by (occurred_at, id). The id is a string that sorts the same in Python and in SQL (collation
"C"): a source name, a colon and its key, with integer keys padded to a fixed width by `entry_id`.
"""

import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

ID_WIDTH = 12  # integer keys are zero-padded to this many digits
DEFAULT_LIMIT = 50  # API 1.5 says 20, but a log is read for what happened lately: the INDEX contract fixes 50
MAX_LIMIT = 100
_CODE = re.compile(r"[a-z0-9_.:-]{1,64}")


class BadCursor(ValueError):
    """A cursor that is not one this reader made."""


@dataclass(frozen=True)
class AuditFilter:
    agent_token_id: UUID | None = None
    campaign_id: UUID | None = None
    before: tuple[datetime, str] | None = None  # entries strictly older than this (occurred_at, id)
    limit: int = DEFAULT_LIMIT


@dataclass(frozen=True)
class AuditEntry:
    id: str
    occurred_at: datetime
    source: str
    kind: str
    actor_type: str
    summary: str
    outcome: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    agent_token_id: UUID | None = None
    campaign_id: UUID | None = None
    organization: dict[str, Any] | None = None  # {id, name}; name is None where the caller may not read it
    link: dict[str, str] | None = None  # {kind, href}


Reader = Callable[[Connection, UUID, AuditFilter], list[AuditEntry]]
_sources: dict[str, Reader] = {}


def source(name: str, reader: Reader) -> None:
    """Register a reader at start-up. A reader returns at most `f.limit` entries, newest first, strictly before
    `f.before`, and `[]` for a filter it cannot honour. A second registration of a name is a mistake and raises."""
    if name in _sources:
        raise ValueError(f"audit source {name!r} is already registered")
    _sources[name] = reader


def entry_id(name: str, key: int | str | UUID) -> str:
    """The entry id of a source's row: `name:key`, an integer key padded so text order is numeric order."""
    return f"{name}:{key:0{ID_WIDTH}d}" if isinstance(key, int) else f"{name}:{key}"


def code(value: Any) -> str | None:
    """A value from a details column when it is a short code (never free text), else None."""
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


BEFORE_CLAUSE = '(({when}), (({entry_id}) COLLATE "C")) < (CAST(:before_at AS timestamptz), CAST(:before_id AS text))'


def before_sql(when: str, entry_id_sql: str, f: AuditFilter) -> tuple[str, dict[str, Any]]:
    """The SQL condition and parameters for `f.before`, so a source cuts the page exactly where `read` does. `when` and
    `entry_id_sql` are the source's own SQL expressions for the entry's time and id (constants, never input)."""
    if f.before is None:
        return "true", {}
    clause = BEFORE_CLAUSE.format(when=when, entry_id=entry_id_sql)
    return clause, {"before_at": f.before[0], "before_id": f.before[1]}


def _encode(entry: AuditEntry) -> str:
    raw = json.dumps({"t": entry.occurred_at.isoformat(), "i": entry.id})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        when, entry = datetime.fromisoformat(data["t"]), data["i"]
        if when.tzinfo is None or not isinstance(entry, str):
            raise ValueError
        return when, entry
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise BadCursor from None


def read(conn: Connection, user_id: UUID, f: AuditFilter) -> tuple[list[AuditEntry], str | None]:
    """The caller's newest `f.limit` entries across every source before the cursor, and the next page's cursor (None
    at the end). Each source is asked for one more than the page, which is how a next page is known to exist."""
    if not 1 <= f.limit <= MAX_LIMIT:
        raise ValueError("limit is out of range")
    ask = replace(f, limit=f.limit + 1)
    merged: list[AuditEntry] = []
    for reader in list(_sources.values()):
        merged += reader(conn, user_id, ask)
    merged.sort(key=lambda e: (e.occurred_at, e.id), reverse=True)
    if f.before is not None:  # a reader that forgot the cut must not repeat entries
        merged = [e for e in merged if (e.occurred_at, e.id) < f.before]
    page = merged[: f.limit]
    return page, _encode(page[-1]) if len(merged) > f.limit and page else None


# ---- the account source: audit_log rows about the caller ----------------------------------------------------------

ACCOUNT_TEXT = {
    "auth.signed_in": "You signed in.",
    "auth.signed_out": "You signed out.",
    "auth.failed": "A sign-in attempt for your account failed.",
    "consent.granted": "You gave a consent.",
    "consent.withdrawn": "You withdrew a consent.",
    "hiring.introduction_accepted": "You accepted an introduction.",
    "hiring.introduction_declined": "You declined an introduction.",
    "introduction.accepted": "You accepted an introduction.",
    "privacy.export_ready": "Your data export was made.",
    "privacy.deletion_requested": "You asked for your account to be deleted.",
    "brief.unsubscribed": "You unsubscribed from the brief email.",
    "agentnet.agent_call": "An outside agent called {tool} with your token.",
    "agentnet.agent_call_refused": "A call from an outside agent was refused: {tool}, {reason}.",
}

ACCOUNT_SQL = """
SELECT a.id, a.created_at, a.event_type, a.actor_type::text AS actor_type, a.agent_token_id, a.organization_id,
       a.details
  FROM audit_log a
 WHERE a.subject_user_id = :u
   AND (CAST(:token AS uuid) IS NULL OR a.agent_token_id = CAST(:token AS uuid))
   AND {before}
 ORDER BY a.created_at DESC, ('account:' || lpad(a.id::text, :width, '0')) COLLATE "C" DESC
 LIMIT :limit"""


def _account(conn: Connection, user_id: UUID, f: AuditFilter) -> list[AuditEntry]:
    if f.campaign_id is not None:  # account events belong to no campaign
        return []
    before, params = before_sql("a.created_at", "'account:' || lpad(a.id::text, :width, '0')", f)
    rows = conn.execute(
        text(ACCOUNT_SQL.format(before=before)),
        {"u": user_id, "token": f.agent_token_id, "limit": f.limit, "width": ID_WIDTH, **params},
    ).all()
    entries = []
    for row in rows:
        details = row.details or {}
        tool, reason = code(details.get("tool")) or "a tool", code(details.get("reason"))
        template = ACCOUNT_TEXT.get(row.event_type, row.event_type)
        refused = row.event_type == "agentnet.agent_call_refused"
        entries.append(
            AuditEntry(
                id=entry_id("account", row.id),
                occurred_at=row.created_at,
                source="account",
                kind=row.event_type,
                actor_type=row.actor_type,
                summary=template.format(tool=tool, reason=reason or "no reason given"),
                outcome="refused" if refused else code(details.get("outcome")),
                reason_codes=[reason] if refused and reason else [],
                agent_token_id=row.agent_token_id,
                organization={"id": str(row.organization_id), "name": None} if row.organization_id else None,
            )
        )
    return entries


source("account", _account)
