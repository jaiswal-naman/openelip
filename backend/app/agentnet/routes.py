"""The agent-token routes (API 2.3 and 2.15; NET-01, NET-03; plan 09-04).

    POST   /v1/me/agent-tokens                          a candidate's token (records the agent_access consent)
    GET    /v1/me/agent-tokens                          the candidate's tokens, with status and refused calls
    GET    /v1/me/agent-tokens/{id}/refusals            when, which tool and why a call with that token was refused
    DELETE /v1/me/agent-tokens/{id}                     revoke (the next call gets 401)
    POST   /v1/orgs/{org_id}/agent-tokens               an organization's token (owner or admin)
    GET    /v1/orgs/{org_id}/agent-tokens               the organization's tokens (owner or admin)
    GET    /v1/orgs/{org_id}/agent-tokens/{id}/refusals the same list for an organization token
    DELETE /v1/orgs/{org_id}/agent-tokens/{id}          revoke

The secret is in the response of the first POST only. The idempotency record keeps the response so a repeat can answer
with it, and that record must not hold a secret, so it is stored without the token and the first answer gets it added.
Tokens are issued by a signed-in person with a session: an agent token never works on /v1.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from app.hiring import access
from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import api, tokens

router = APIRouter()

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]
CandidateScope = Literal["matches:read", "explanations:read"]
OrgScope = Literal["twin:ask", "introductions:request"]
Status = Literal["active", "revoked", "expired"]
CLIENT_NAME_MAX = 80


class CandidateTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(min_length=1, max_length=CLIENT_NAME_MAX)
    scopes: list[CandidateScope] = Field(min_length=1)
    expires_in_days: int = Field(default=tokens.DEFAULT_DAYS, ge=tokens.MIN_DAYS, le=tokens.MAX_DAYS)
    consent_text_version: str = Field(min_length=1, max_length=100)


class OrgTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(min_length=1, max_length=CLIENT_NAME_MAX)
    scopes: list[OrgScope] = Field(min_length=1)
    expires_in_days: int = Field(default=tokens.DEFAULT_DAYS, ge=tokens.MIN_DAYS, le=tokens.MAX_DAYS)


class TokenIssued(BaseModel):
    id: UUID
    token: str | None  # the secret, in the first answer only
    is_shown_once: bool
    principal: Literal["candidate", "organization"]
    client_name: str
    scopes: list[str]
    status: Status
    expires_at: datetime
    mcp_path: str = "/mcp"


class TokenItem(BaseModel):
    id: UUID
    client_name: str
    principal: Literal["candidate", "organization"]
    scopes: list[str]
    status: Status
    expires_at: datetime
    last_used_at: datetime | None
    created_at: datetime
    refused_last_7_days: int


class TokenList(BaseModel):
    tokens: list[TokenItem]


class RefusalItem(BaseModel):
    created_at: datetime
    tool: str | None
    reason: str | None


class RefusalList(BaseModel):
    refusals: list[RefusalItem]


def _issued_view(issued: tokens.Issued, *, secret: bool) -> dict[str, Any]:
    return {
        "id": issued.id,
        "token": issued.secret if secret else None,
        "is_shown_once": secret,
        "principal": issued.principal,
        "client_name": issued.client_name,
        "scopes": list(issued.scopes),
        "status": "active",
        "expires_at": issued.expires_at,
        "mcp_path": "/mcp",
    }


def _issue(write: Write, make: Any) -> Response:
    """Run the issue once per Idempotency-Key and add the secret to the first answer only."""
    made: list[tokens.Issued] = []

    def effect(conn: Connection) -> dict[str, Any]:
        try:
            issued = make(conn)
        except api.InvalidToken as error:
            raise ApiError(422, "validation_failed", str(error)) from None
        made.append(issued)
        return _issued_view(issued, secret=False)  # what the idempotency record keeps: no secret

    response = write.run(effect, status=201)
    if not made:  # a repeat of the key: the stored answer, with no secret
        return response
    body = json.loads(bytes(response.body))
    body["data"]["token"] = made[0].secret
    body["data"]["is_shown_once"] = True
    return JSONResponse(body, status_code=201, headers={"Cache-Control": "no-store"})


def _items(rows: list[tokens.TokenRow]) -> dict[str, Any]:
    return {"tokens": [{**row.__dict__, "scopes": list(row.scopes)} for row in rows]}


def _refusals(found: list[tokens.Refusal] | None) -> dict[str, Any]:
    if found is None:
        raise ApiError(404, "not_found", "No such resource.")
    return {"refusals": [r.__dict__ for r in found]}


# ---- a candidate's own tokens ---------------------------------------------------------------------------------------


@router.post("/v1/me/agent-tokens", status_code=201, response_model=Envelope[TokenIssued])
def issue_my_agent_token(body: CandidateTokenIn, write: Idempotent) -> Response:
    return _issue(
        write,
        lambda conn: api.issue_candidate_token(
            conn, write.user_id, body.client_name, body.scopes, body.expires_in_days, body.consent_text_version
        ),
    )


@router.get("/v1/me/agent-tokens", response_model=Envelope[TokenList])
def list_my_agent_tokens(who: Signed) -> dict[str, Any]:
    with db.transaction(who.user_id) as conn:
        return envelope(_items(tokens.list_tokens(conn, principal="candidate", owner_id=who.user_id)))


@router.get("/v1/me/agent-tokens/{token_id}/refusals", response_model=Envelope[RefusalList])
def my_agent_token_refusals(token_id: UUID, who: Signed) -> dict[str, Any]:
    with db.transaction(who.user_id) as conn:
        return envelope(
            _refusals(tokens.refusals(conn, principal="candidate", owner_id=who.user_id, token_id=token_id))
        )


@router.delete("/v1/me/agent-tokens/{token_id}", status_code=204)
def revoke_my_agent_token(token_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> None:
        found = tokens.revoke(
            conn, principal="candidate", owner_id=write.user_id, token_id=token_id, user_id=write.user_id
        )
        if not found:
            raise ApiError(404, "not_found", "No such resource.")

    return write.run(effect, status=204)


# ---- an organization's tokens ---------------------------------------------------------------------------------------


@router.post("/v1/orgs/{org_id}/agent-tokens", status_code=201, response_model=Envelope[TokenIssued])
def issue_org_agent_token(org_id: UUID, body: OrgTokenIn, write: Idempotent) -> Response:
    return _issue(
        write,
        lambda conn: api.issue_org_token(
            conn, org_id, write.user_id, body.client_name, body.scopes, body.expires_in_days
        ),
    )


@router.get("/v1/orgs/{org_id}/agent-tokens", response_model=Envelope[TokenList])
def list_org_agent_tokens(org_id: UUID, who: Signed) -> dict[str, Any]:
    with db.transaction(who.user_id) as conn:
        access.require_admin(conn, org_id)
        return envelope(_items(tokens.list_tokens(conn, principal="organization", owner_id=org_id)))


@router.get("/v1/orgs/{org_id}/agent-tokens/{token_id}/refusals", response_model=Envelope[RefusalList])
def org_agent_token_refusals(org_id: UUID, token_id: UUID, who: Signed) -> dict[str, Any]:
    with db.transaction(who.user_id) as conn:
        access.require_admin(conn, org_id)
        return envelope(_refusals(tokens.refusals(conn, principal="organization", owner_id=org_id, token_id=token_id)))


@router.delete("/v1/orgs/{org_id}/agent-tokens/{token_id}", status_code=204)
def revoke_org_agent_token(org_id: UUID, token_id: UUID, write: Idempotent) -> Response:
    def effect(conn: Connection) -> None:
        access.require_admin(conn, org_id)
        if not tokens.revoke(conn, principal="organization", owner_id=org_id, token_id=token_id, user_id=write.user_id):
            raise ApiError(404, "not_found", "No such resource.")

    return write.run(effect, status=204)
