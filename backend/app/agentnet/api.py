"""What other modules and tooling may call from app.agentnet: the two ways to issue a token (plan 09-04).

  issue_candidate_token(conn, user_id, client_name, scopes, days, consent_text_version)   a candidate's own token
  issue_org_token(conn, organization_id, user_id, client_name, scopes, days)              an organization's token

Both run on the caller's connection and API login, as the routes do (the demo seed and the wind tunnel use them), and
return `Issued(id, secret, ...)`: the secret exists only in that value. A stale consent wording is
`trust.api.ConsentRequired` (403 consent_required); a scope outside the principal's set or a number of days outside 1 to
90 is `InvalidToken`.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Connection

from app.hiring import access
from app.trust import api as trust

from . import tokens
from .tokens import AGENT_ACCESS_TEXT_VERSION, Issued

__all__ = ["AGENT_ACCESS_TEXT_VERSION", "InvalidToken", "Issued", "issue_candidate_token", "issue_org_token"]


class InvalidToken(ValueError):
    """The scopes or the lifetime asked for are not allowed for this principal."""


def _check(scopes: Sequence[str], allowed: tuple[str, ...], days: int) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(scopes))
    if not unique or any(scope not in allowed for scope in unique):
        raise InvalidToken("scopes must be a non-empty subset of " + ", ".join(allowed))
    if not tokens.MIN_DAYS <= days <= tokens.MAX_DAYS:
        raise InvalidToken(f"days must be from {tokens.MIN_DAYS} to {tokens.MAX_DAYS}")
    return unique


def issue_candidate_token(
    conn: Connection,
    user_id: UUID,
    client_name: str,
    scopes: Sequence[str],
    days: int,
    consent_text_version: str,
) -> Issued:
    """The candidate's token, with its agent_access consent recorded in the same transaction. `user_id` must be the
    signed-in person (the database refuses another)."""
    if consent_text_version != AGENT_ACCESS_TEXT_VERSION:
        raise trust.ConsentRequired("missing", "agent_access")
    scope_set = _check(scopes, tokens.CANDIDATE_SCOPES, days)
    consent_id = tokens.grant_agent_access(conn, user_id, consent_text_version)
    return tokens.issue(
        conn,
        principal="candidate",
        user_id=user_id,
        organization_id=None,
        consent_id=consent_id,
        client_name=client_name,
        scopes=scope_set,
        days=days,
    )


def issue_org_token(
    conn: Connection,
    organization_id: UUID,
    user_id: UUID,
    client_name: str,
    scopes: Sequence[str],
    days: int,
) -> Issued:
    """The organization's token, issued by an owner or admin (404 for a stranger, 403 for a plain member). The token
    stops working if its issuer leaves the organization."""
    access.require_admin(conn, organization_id)
    scope_set = _check(scopes, tokens.ORG_SCOPES, days)
    return tokens.issue(
        conn,
        principal="organization",
        user_id=user_id,
        organization_id=organization_id,
        consent_id=None,
        client_name=client_name,
        scopes=scope_set,
        days=days,
    )
