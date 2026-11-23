"""The gate in front of the MCP SDK's app: every check that must happen before the SDK sees a request (NET-03; API 5.2,
5.6; INDEX "MCP endpoint").

A pure ASGI wrapper. For each request, in this order:

  1. the method: only POST (405 with Allow: POST)
  2. the Host header, then the Origin header when present: both must be on this computer's allowlist (421, 403)
  3. the bearer token, from the Authorization header only (never a query string, never a cookie): 401 with
     `WWW-Authenticate: Bearer error="invalid_token"` for none, an unknown, revoked or expired token, a withdrawn
     consent or an issuer who left the organization (resolve_agent_token decides, on every request)
  4. the token's limits (429 with Retry-After; a refusal row rate_limited)
  5. the body, read up to MAX_BODY_BYTES (413) and parsed: it must be one JSON-RPC object, not a batch (400)
  6. for tools/call: an Mcp-Name header that differs from the body is 400 with JSON-RPC error -32020; an unknown tool
     or arguments that do not fit the tool's input model are JSON-RPC error -32602; a tool outside the token's scopes
     is 403 with `WWW-Authenticate: Bearer error="insufficient_scope", scope="..."`. Each refusal is an audit row.
  7. the body is handed to the SDK's app unchanged.

The SDK's own transport checks (and its auth middleware, which resolves the token again for `get_access_token()`) still
run after this. Nothing here logs a token, a question or a body: log lines hold token ids, tool names, codes and counts.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
from mcp.types import HEADER_MISMATCH, INVALID_PARAMS, INVALID_REQUEST
from sqlalchemy import text
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform import db, log

from . import audit, limits, tokens
from .audit import Caller

MAX_BODY_BYTES = 65_536  # assumption: a question is at most 1,000 characters, so this is generous
BEARER = "bearer "


def resolve(secret: str) -> Caller | None:
    """The token behind a bearer secret, or None. The lookup is by the secret's SHA-256, in a transaction with no user:
    resolve_agent_token is for the API login before any request context exists (DATA_MODEL 5.2)."""
    if not secret.startswith("agt_"):
        return None
    with db.transaction() as conn:
        row = conn.execute(
            text(
                "SELECT token_id, user_id, organization_id, CAST(scopes AS text[]) AS scopes FROM "
                "resolve_agent_token(:h)"
            ),
            {"h": tokens.digest(secret)},
        ).one_or_none()
    if row is None:
        return None
    return Caller(row.token_id, row.user_id, row.organization_id, tuple(row.scopes))


def bearer_secret(headers: Headers) -> str | None:
    """The secret of a single `Authorization: Bearer ...` header, else None."""
    values = headers.getlist("authorization")
    if len(values) != 1 or not values[0].lower().startswith(BEARER):
        return None
    return values[0][len(BEARER) :].strip() or None


class Gate:
    """`tools` maps a tool name to (scopes, any one of which allows it, and the function that validates its arguments,
    which raises ValueError). `app` is the SDK's Starlette app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        tools: dict[str, tuple[tuple[str, ...], Callable[[dict[str, Any]], object]]],
        allowed_origins: tuple[str, ...],
        limiter: limits.Limits,
    ) -> None:
        self.app = app
        self.tools = tools
        self.origins = {origin.lower() for origin in allowed_origins}
        self.hosts = {urlsplit(origin).netloc.lower() for origin in allowed_origins}
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await Response(status_code=404)(scope, receive, send)
            return
        headers = Headers(scope=scope)
        early = self._early_refusal(scope, headers)
        if early is not None:
            await early(scope, receive, send)
            return
        secret = bearer_secret(headers)
        caller = await anyio.to_thread.run_sync(resolve, secret) if secret else None
        if caller is None:
            client = (scope.get("client") or ("unknown", 0))[0]
            seen = self.limiter.unattributed(hashlib.sha256(client.encode()).hexdigest()[:8])
            log.event("agentnet.unauthenticated", outcome="invalid_token", count=seen)
            await _unauthorized()(scope, receive, send)
            return
        late = await self._attributed(scope, receive, send, caller, headers)
        if late is not None:
            await late(scope, receive, send)

    def _early_refusal(self, scope: Scope, headers: Headers) -> Response | None:
        if scope["method"] != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        host = headers.get("host", "").lower()
        if host not in self.hosts:
            return Response("Invalid Host header", status_code=421)
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.origins:
            return Response("Invalid Origin header", status_code=403)
        return None

    async def _attributed(
        self, scope: Scope, receive: Receive, send: Send, caller: Caller, headers: Headers
    ) -> Response | None:
        """Checks 4 to 7 for a request with a live token. Returns the refusal to send, or None after serving."""
        body = await _read_body(receive)
        if body is None:
            await anyio.to_thread.run_sync(self._refuse, caller, None, "invalid_input")
            return Response("Request body too large", status_code=413)
        message = _parse(body)
        tool = _tool_name(message)
        wait = self.limiter.admit(caller.token_id, tool)
        if wait is not None:
            await anyio.to_thread.run_sync(audit.refused, caller, tool, "rate_limited")
            log.event("agentnet.refused", token_id=caller.token_id, kind=tool or "", outcome="rate_limited")
            return Response(status_code=429, headers={"Retry-After": str(wait)})
        if not isinstance(message, dict):
            await anyio.to_thread.run_sync(self._refuse, caller, None, "invalid_input")
            return _rpc_error(None, INVALID_REQUEST, "Send one JSON-RPC request object.", 400)
        if message.get("method") == "tools/call":
            refusal = await self._tool_call_refusal(caller, headers, message)
            if refusal is not None:
                return refusal

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
        return None

    async def _tool_call_refusal(self, caller: Caller, headers: Headers, message: dict[str, Any]) -> Response | None:
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        request_id = message.get("id")
        named = headers.get("mcp-name")
        if named is not None and named != name:
            await anyio.to_thread.run_sync(self._refuse, caller, None, "invalid_input")
            return _rpc_error(request_id, HEADER_MISMATCH, "The Mcp-Name header does not match the request.", 400)
        spec = self.tools.get(name) if isinstance(name, str) else None
        if spec is None:
            await anyio.to_thread.run_sync(self._refuse, caller, None, "invalid_input")
            return _rpc_error(request_id, INVALID_PARAMS, "Unknown tool.", 200)
        needed, validate = spec
        assert isinstance(name, str)
        if not any(scope in caller.scopes for scope in needed):
            await anyio.to_thread.run_sync(self._refuse, caller, name, "insufficient_scope")
            return Response(
                status_code=403,
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{needed[0]}"'},
            )
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            validate(arguments)
        except ValueError:
            await anyio.to_thread.run_sync(self._refuse, caller, name, "invalid_input")
            return _rpc_error(request_id, INVALID_PARAMS, "The arguments do not match the tool's input.", 200)
        return None

    def _refuse(self, caller: Caller, tool: str | None, reason: str) -> None:
        """An audit row and one count toward the token's hour; runs in a worker thread."""
        audit.refused(caller, tool, reason)
        self.limiter.refused(caller.token_id)
        log.event("agentnet.refused", token_id=caller.token_id, kind=tool or "", outcome=reason)


def _unauthorized() -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def _rpc_error(request_id: object, code: int, message: str, status: int) -> Response:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, status_code=status
    )


async def _read_body(receive: Receive) -> bytes | None:
    """The whole request body, or None when it is longer than MAX_BODY_BYTES."""
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            message = await receive()
        except ClientDisconnect:
            return b""
        if message["type"] != "http.request":
            return b""
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _parse(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _tool_name(message: object) -> str | None:
    if isinstance(message, dict) and message.get("method") == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        return name if isinstance(name, str) else None
    return None
