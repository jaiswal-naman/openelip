"""Invented rows for the Twin and agent-token tests (09-01), for 09-03, 09-04 and 09-06 to reuse.

    from tests.twin.factories import twin_world, twin_session, employer_question, candidate_token, org_token

    world = twin_world(conn)                       # "Halden Demo", an owner and a plain member, an open role, a
                                                   # candidate who accepted it (consent live)
    session = twin_session(conn, world)            # an open session on that introduction
    question = employer_question(conn, session)    # an employer message: topic NULL, the Twin has not answered
    token = candidate_token(conn, world.candidate.id)   # Issued(id, secret): the secret is shown once, as in the API
    org = org_token(conn, world.org, world.org.owner)

Everything is made up (the organization ends in "Demo", addresses are example.test). Like the Phase 6 and 7 factories:
every one refuses a database whose name does not end in _test, writes with plain SQL on the connection the test gives
(the bootstrap login of the `conn` fixture, to which row-level security does not apply, so the guards of the schema
still run), and must be called before `act_as`. A token's secret is `agt_` and 32 URL-safe characters; only its sha256
is stored, as the issue code will do.
"""

import hashlib
import secrets
from collections.abc import Sequence
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import Connection, text

from tests.hiring.factories import (
    Accepted,
    Candidate,
    Member,
    Org,
    Role,
    _require_test_database,
    make_accepted,
    make_candidate,
    make_member,
    make_open_role,
    make_org,
)

AGENT_CONSENT_TEXT = "test-agent-v1"  # the wording of the agent_access consent, invented


class Issued(NamedTuple):
    id: UUID
    secret: str


class TwinWorld(NamedTuple):
    org: Org
    member: Member  # a plain member of the organization
    role: Role
    candidate: Candidate
    accepted: Accepted

    @property
    def introduction(self) -> UUID:
        return self.accepted.id


def twin_world(conn: Connection) -> TwinWorld:
    """An organization with an owner and a plain member, an open role, and a candidate who accepted it."""
    _require_test_database(conn)
    org = make_org(conn)
    member = make_member(conn, org)
    role = make_open_role(conn, org)
    candidate = make_candidate(conn)
    return TwinWorld(org, member, role, candidate, make_accepted(conn, role, candidate))


def twin_session(
    conn: Connection,
    world: TwinWorld,
    *,
    opened_by: UUID | None = None,
    agent_token_id: UUID | None = None,
    status: str = "open",
) -> UUID:
    """A Twin session on the world's accepted introduction. `status="closed"` closes it after the insert."""
    _require_test_database(conn)
    session: UUID = conn.execute(
        text(
            """INSERT INTO twin_sessions (introduction_id, user_id, organization_id, opened_by, agent_token_id)
               VALUES (:i, :u, :o, :b, :t) RETURNING id"""
        ),
        {
            "i": world.introduction,
            "u": world.candidate.id,
            "o": world.org.id,
            "b": opened_by if opened_by is not None else world.member.user,
            "t": agent_token_id,
        },
    ).scalar_one()
    if status == "closed":
        conn.execute(
            text("UPDATE twin_sessions SET status = 'closed', closed_at = now() WHERE id = :s"), {"s": session}
        )
    return session


def employer_question(conn: Connection, session_id: UUID, body: str = "What did you build at your last job?") -> UUID:
    """An employer's question, stored the way `twin.api.ask` stores it: no topic, no Twin message yet."""
    _require_test_database(conn)
    message: UUID = conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
               SELECT s.id, s.user_id, s.organization_id, 'employer', :b FROM twin_sessions s WHERE s.id = :s
               RETURNING id"""
        ),
        {"s": session_id, "b": body},
    ).scalar_one()
    return message


def _secret() -> tuple[str, bytes]:
    secret = "agt_" + secrets.token_urlsafe(24)  # 4 + 32 characters
    return secret, hashlib.sha256(secret.encode()).digest()


def candidate_token(
    conn: Connection,
    user_id: UUID,
    scopes: Sequence[str] = ("matches:read", "explanations:read"),
    *,
    expires_in: str = "30 days",
    client_name: str = "Demo agent client",
) -> Issued:
    """A candidate's token with its own live agent_access consent (granted through trust.api, as the route will)."""
    _require_test_database(conn)
    consent: UUID = conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, text_version)
               VALUES (:u, 'agent_access', 'granted', :v) RETURNING id"""
        ),
        {"u": user_id, "v": AGENT_CONSENT_TEXT},
    ).scalar_one()
    secret, digest = _secret()
    token: UUID = conn.execute(
        text(
            """INSERT INTO agent_tokens (principal, user_id, consent_id, client_name, token_sha256, scopes, expires_at)
               VALUES ('candidate', :u, :c, :n, :h, CAST(:s AS token_scope[]), now() + CAST(:e AS interval))
               RETURNING id"""
        ),
        {"u": user_id, "c": consent, "n": client_name, "h": digest, "s": list(scopes), "e": expires_in},
    ).scalar_one()
    return Issued(token, secret)


def org_token(
    conn: Connection,
    org: Org,
    issuer: UUID,
    scopes: Sequence[str] = ("twin:ask", "introductions:request"),
    *,
    expires_in: str = "30 days",
    client_name: str = "Demo agent client",
) -> Issued:
    """An organization's token issued by `issuer`, who must be an owner or admin (the schema checks it)."""
    _require_test_database(conn)
    secret, digest = _secret()
    token: UUID = conn.execute(
        text(
            """INSERT INTO agent_tokens (principal, user_id, organization_id, client_name, token_sha256, scopes,
                                         expires_at)
               VALUES ('organization', :u, :o, :n, :h, CAST(:s AS token_scope[]), now() + CAST(:e AS interval))
               RETURNING id"""
        ),
        {"u": issuer, "o": org.id, "n": client_name, "h": digest, "s": list(scopes), "e": expires_in},
    ).scalar_one()
    return Issued(token, secret)


def withdraw(conn: Connection, consent_id: UUID) -> None:
    """Withdraw one grant (the introduction consent of an accepted introduction, or a token's agent_access grant): an
    append-only withdrawal row pointing at it, as trust.api writes. `trust.withdraw_consent` refuses these two
    purposes, since their own actions record them."""
    _require_test_database(conn)
    conn.execute(
        text(
            """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version, organization_id, role_id,
                                     created_at)
               SELECT g.user_id, g.purpose, 'withdrawn', g.id, g.text_version, g.organization_id, g.role_id,
                      clock_timestamp()
               FROM consents g WHERE g.id = :c"""
        ),
        {"c": consent_id},
    )
