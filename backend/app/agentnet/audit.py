"""The audit rows of agent calls (TRU-03, NET-03; INDEX "Audit log").

`allowed` writes `agentnet.agent_call` (details: tool and outcome `allowed` or `declined`) in the call's own
transaction,
and touches `last_used_at`. `refused` writes `agentnet.agent_call_refused` (details: tool and reason code) in a fresh
transaction under the token's context, so the row stays when the call's own work rolls back. Both are written by the API
login under the token's own context, which the audit_log insert policy accepts when the actor is the token's issuer and
the agent_token_id is the token's; actor_type is `outside_agent`.

A row holds ids and codes: never the question, a message body or the token. The subject is the candidate the call is
about: the issuer for a candidate token, and, for an organization token's ask_twin and get_twin_answer, the candidate
whose accepted introduction the call used (policy: the organization must hold an introduction with that person).
"""

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import db

Principal = Literal["candidate", "organization"]


@dataclass(frozen=True)
class Caller:
    """Who is calling: the token, its issuer, the organization (organization tokens only) and the scopes it holds."""

    token_id: UUID
    user_id: UUID  # the issuer: the candidate, or the owner or admin who made the organization's token
    organization_id: UUID | None
    scopes: tuple[str, ...]

    @property
    def principal(self) -> Principal:
        return "organization" if self.organization_id is not None else "candidate"

    @property
    def context(self) -> db.AgentContext:
        return db.AgentContext(self.token_id, self.scopes)

    def transaction(self) -> AbstractContextManager[Connection]:
        """A transaction in this token's request context: row-level security decides what it may read and write."""
        return db.transaction(self.user_id, agent=self.context)


_INSERT = text(
    """INSERT INTO audit_log (event_type, actor_type, actor_user_id, subject_user_id, organization_id, agent_token_id,
                              entity_table, entity_id, details)
       VALUES (:event, 'outside_agent', :actor, :subject, :org, :token, :table, :entity, CAST(:details AS jsonb))"""
)


def _write(
    conn: Connection,
    caller: Caller,
    event: str,
    details: dict[str, str],
    subject: UUID | None,
    entity_table: str | None,
    entity_id: str | None,
) -> None:
    conn.execute(
        _INSERT,
        {
            "event": event,
            "actor": caller.user_id,
            "subject": subject,
            "org": caller.organization_id,
            "token": caller.token_id,
            "table": entity_table,
            "entity": entity_id,
            "details": json.dumps(details),
        },
    )


def _subject(caller: Caller, subject_user_id: UUID | None) -> UUID | None:
    return caller.user_id if caller.principal == "candidate" else subject_user_id


def allowed(
    conn: Connection,
    caller: Caller,
    tool: str,
    outcome: Literal["allowed", "declined"] = "allowed",
    *,
    subject_user_id: UUID | None = None,
    entity_table: str | None = None,
    entity_id: str | None = None,
) -> None:
    """One agent_call row in the caller's transaction, and last_used_at (at most once a minute) in the same one."""
    _write(
        conn,
        caller,
        "agentnet.agent_call",
        {"tool": tool, "outcome": outcome},
        _subject(caller, subject_user_id),
        entity_table,
        entity_id,
    )
    conn.execute(text("SELECT touch_agent_token(:t)"), {"t": caller.token_id})


def refused(caller: Caller, tool: str | None, reason: str, *, subject_user_id: UUID | None = None) -> None:
    """One agent_call_refused row in a transaction of its own. `tool` is None only for a call that named no tool."""
    with caller.transaction() as conn:
        _write(
            conn,
            caller,
            "agentnet.agent_call_refused",
            {"tool": tool or "", "reason": reason},
            _subject(caller, subject_user_id),
            None,
            None,
        )
