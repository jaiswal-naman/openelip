"""Agent tokens: make one, list them, revoke one, read what it was refused (NET-01, NET-03; API 5.3).

A token is `agt_` plus 32 URL-safe characters from `secrets` (192 bits). The secret is handed over once, when it is
made; the table holds only its SHA-256, so neither the database, a backup nor a log can give it back. Every function
runs on the caller's own connection and API login, under row-level security: a candidate reads and revokes their own
tokens, an owner or admin the organization's, and nobody else's.

A candidate token carries its own `agent_access` consent, a new grant for each token, so withdrawing it (revoking the
token writes the withdrawal) ends that token and no other. trust.consent records only the purposes a person gives in
settings, and agent_access is not one of them, so the grant is inserted here, with the audit row.
"""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.trust import api as trust

AGENT_ACCESS_TEXT_VERSION = "agent-access-2026-11"  # the wording of the consent sheet the candidate agrees to
DEFAULT_DAYS = 30
MIN_DAYS = 1
MAX_DAYS = 90
CANDIDATE_SCOPES = ("matches:read", "explanations:read")
ORG_SCOPES = ("twin:ask", "introductions:request")
REFUSALS_SHOWN = 100  # the most recent refused calls the owner of a token can read
REFUSAL_WINDOW_DAYS = 7

Principal = Literal["candidate", "organization"]
Status = Literal["active", "revoked", "expired"]


@dataclass(frozen=True)
class Issued:
    """A token just made. `secret` exists only here: the caller shows it once and forgets it."""

    id: UUID
    secret: str
    principal: Principal
    client_name: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class TokenRow:
    id: UUID
    client_name: str
    principal: Principal
    scopes: tuple[str, ...]
    status: Status
    expires_at: datetime
    last_used_at: datetime | None
    created_at: datetime
    refused_last_7_days: int


@dataclass(frozen=True)
class Refusal:
    created_at: datetime
    tool: str | None
    reason: str | None


def new_secret() -> str:
    return "agt_" + secrets.token_urlsafe(24)  # 24 random bytes encode to 32 characters


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def status_of(revoked_at: datetime | None, expires_at: datetime, now: datetime) -> Status:
    """Derived, never stored: revoked wins over expired."""
    if revoked_at is not None:
        return "revoked"
    return "expired" if expires_at <= now else "active"


def grant_agent_access(conn: Connection, user_id: UUID, text_version: str) -> UUID:
    """A new agent_access grant for the signed-in candidate, audited. Returns the consent's id."""
    consent_id: UUID = conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, text_version, created_at)
               VALUES (:u, 'agent_access', 'granted', :v, clock_timestamp()) RETURNING id"""
        ),
        {"u": user_id, "v": text_version},
    ).scalar_one()
    trust.audit_log(
        conn,
        "consent.granted",
        "candidate",
        user_id,
        {"purpose": "agent_access", "text_version": text_version},
        entity_table="consents",
        entity_id=str(consent_id),
    )
    return consent_id


def issue(
    conn: Connection,
    *,
    principal: Principal,
    user_id: UUID,
    organization_id: UUID | None,
    consent_id: UUID | None,
    client_name: str,
    scopes: Sequence[str],
    days: int,
) -> Issued:
    """Store a new token (its hash only) and audit it. `user_id` is the issuer: the candidate, or the owner or admin."""
    secret = new_secret()
    row = conn.execute(
        text(
            """INSERT INTO agent_tokens (principal, user_id, organization_id, consent_id, client_name, token_sha256,
                                         scopes, expires_at)
               VALUES (CAST(:p AS token_principal), :u, :o, :c, :n, :h, CAST(:s AS token_scope[]),
                       now() + make_interval(days => :d))
               RETURNING id, expires_at"""
        ),
        {
            "p": principal,
            "u": user_id,
            "o": organization_id,
            "c": consent_id,
            "n": client_name,
            "h": digest(secret),
            "s": list(scopes),
            "d": days,
        },
    ).one()
    trust.audit_log(
        conn,
        "agentnet.token_issued",
        "candidate" if principal == "candidate" else "employer",
        user_id if principal == "candidate" else None,
        {"scope_count": len(scopes), "days": days},
        actor_user_id=user_id,
        entity_table="agent_tokens",
        entity_id=str(row.id),
        organization_id=organization_id,
    )
    return Issued(row.id, secret, principal, client_name, tuple(scopes), row.expires_at)


_LIST = """SELECT t.id, t.client_name, t.principal::text AS principal, CAST(t.scopes AS text[]) AS scopes,
                  t.revoked_at, t.expires_at, t.last_used_at, t.created_at,
                  (SELECT count(*) FROM audit_log a
                    WHERE a.agent_token_id = t.id AND a.event_type = 'agentnet.agent_call_refused'
                      AND a.created_at > now() - make_interval(days => :window)) AS refused
             FROM agent_tokens t
            WHERE t.principal = CAST(:p AS token_principal) AND {owner}"""


def _owner_clause(principal: Principal) -> str:
    return "t.user_id = :owner" if principal == "candidate" else "t.organization_id = :owner"


def list_tokens(conn: Connection, *, principal: Principal, owner_id: UUID) -> list[TokenRow]:
    """The candidate's tokens (`owner_id` is the candidate) or the organization's (`owner_id` is the organization),
    newest
    first, with the derived status and how many refusals the last seven days hold. Never the secret or its hash. The
    refusals count what the caller may read: the candidate reads their own tokens' rows, an organization admin the
    organization's (policy org_token_calls)."""
    now = datetime.now(UTC)
    rows = conn.execute(
        text(_LIST.format(owner=_owner_clause(principal)) + " ORDER BY t.created_at DESC, t.id"),
        {"p": principal, "owner": owner_id, "window": REFUSAL_WINDOW_DAYS},
    )
    return [
        TokenRow(
            id=r.id,
            client_name=r.client_name,
            principal=r.principal,
            scopes=tuple(r.scopes),
            status=status_of(r.revoked_at, r.expires_at, now),
            expires_at=r.expires_at,
            last_used_at=r.last_used_at,
            created_at=r.created_at,
            refused_last_7_days=r.refused,
        )
        for r in rows
    ]


def revoke(conn: Connection, *, principal: Principal, owner_id: UUID, token_id: UUID, user_id: UUID) -> bool:
    """Set revoked_at (once) on a token of this candidate or organization; False when there is no such token. A
    candidate token's consent is withdrawn in the same transaction. Revoking a revoked token changes nothing."""
    row = conn.execute(
        text(
            f"""SELECT t.id, t.consent_id, t.organization_id, t.revoked_at FROM agent_tokens t
                 WHERE t.id = :id AND t.principal = CAST(:p AS token_principal) AND {_owner_clause(principal)}"""
        ),
        {"id": token_id, "p": principal, "owner": owner_id},
    ).one_or_none()
    if row is None:
        return False
    if row.revoked_at is not None:
        return True
    conn.execute(text("UPDATE agent_tokens SET revoked_at = now() WHERE id = :id"), {"id": token_id})
    if row.consent_id is not None:
        conn.execute(
            text(
                """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version, created_at)
                   SELECT g.user_id, g.purpose, 'withdrawn', g.id, g.text_version, clock_timestamp()
                     FROM consents g WHERE g.id = :c"""
            ),
            {"c": row.consent_id},
        )
    trust.audit_log(
        conn,
        "agentnet.token_revoked",
        "candidate" if principal == "candidate" else "employer",
        user_id if principal == "candidate" else None,
        {},
        actor_user_id=user_id,
        entity_table="agent_tokens",
        entity_id=str(token_id),
        organization_id=row.organization_id,
    )
    return True


def refusals(conn: Connection, *, principal: Principal, owner_id: UUID, token_id: UUID) -> list[Refusal] | None:
    """The token's most recent refused calls: time, tool and reason, nothing else. None when the token is not the
    caller's (the route answers 404)."""
    if not any(t.id == token_id for t in list_tokens(conn, principal=principal, owner_id=owner_id)):
        return None
    rows = conn.execute(
        text(
            """SELECT a.created_at, a.details->>'tool' AS tool, a.details->>'reason' AS reason
                 FROM audit_log a
                WHERE a.agent_token_id = :id AND a.event_type = 'agentnet.agent_call_refused'
                ORDER BY a.created_at DESC, a.id DESC LIMIT :n"""
        ),
        {"id": token_id, "n": REFUSALS_SHOWN},
    )
    return [Refusal(r.created_at, r.tool, r.reason) for r in rows]
