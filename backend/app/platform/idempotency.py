"""Idempotency keys on writes (API.md 1.6, ARCHITECTURE 5.4; requirement PLT-03).

A signed-in write declares the dependency and does its work inside `write.run(...)`:

    @router.post("/things", status_code=201)
    def make_thing(body: ThingIn, write: Annotated[Write, Depends(idempotent)]) -> Response:
        return write.run(lambda conn: things.create(conn, write.user_id, body), status=201)

run() records the key, the effect and the response in ONE transaction. A repeat of the same key gets the first response
and changes nothing; if the effect fails nothing was recorded, so the client may simply retry.

The two public auth writes are exempt (API.md 1.6: their code plays the key's role), and idempotency_keys.user_id is
NOT NULL, so nothing is stored for a caller who is not signed in. They still go through `json_only`.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Connection, text

from . import db, log
from .api import envelope
from .errors import ApiError
from .sessions import Principal, current_user

KEY_PATTERN = re.compile(r"[\x21-\x7e]{8,128}")  # 8 to 128 visible ASCII characters
KEY_LIFETIME = timedelta(hours=24)  # assumption (API.md 1.6)


def lock_name(user_id: UUID, key: str) -> str:
    """The advisory lock that is held while the first request with a key runs."""
    return f"idempotency:{user_id}:{key}"


def json_only(request: Request) -> None:
    """Refuse a write whose body is not JSON (415). Together with SameSite=Lax and the required key, this is what keeps
    a cross-site form from writing (ARCHITECTURE 6.2). A write with no body at all, such as a DELETE, passes."""
    declared = request.headers.get("content-type")
    has_body = request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    if declared is None and not has_body:
        return
    if (declared or "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send the request as application/json.")


async def idempotent(
    request: Request,
    principal: Annotated[Principal, Depends(current_user)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> "Write":
    """Every signed-in write route declares this: 401 without a session, 415 without JSON, 400 without a key."""
    json_only(request)
    if idempotency_key is None:
        raise ApiError(400, "idempotency_key_missing", "Send an Idempotency-Key header with every write.")
    key = idempotency_key.strip().strip('"')  # the draft defines the value as a quoted string; the quotes are ignored
    if not KEY_PATTERN.fullmatch(key):
        raise ApiError(400, "invalid_request", "The Idempotency-Key must be 8 to 128 visible characters.")
    return Write(principal.user_id, key, request.method, request.url.path, _fingerprint(await request.body()))


def _fingerprint(body: bytes) -> bytes:
    """SHA-256 of the canonical JSON body, so key order and spacing do not make a repeat look different."""
    try:
        canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except ValueError:  # no body, or not JSON: the route will say so; hash what was sent
        canonical = body
    return hashlib.sha256(canonical).digest()


@dataclass(frozen=True)
class Write:
    user_id: UUID
    key: str
    method: str
    path: str
    body_sha256: bytes

    def run(self, effect: Callable[[Connection], Any], status: int = 200) -> Response:
        """Run `effect(conn)` once for this key and answer with its data in the envelope (204: no body).

        A repeat gets the stored status and body, with meta.idempotent_replay set, and `effect` does not run.
        """
        with db.transaction(self.user_id) as conn:
            replay = self._claim(conn)
            if replay is not None:
                return replay
            data = effect(conn)
            body = None if status == 204 else jsonable_encoder(envelope(data))
            conn.execute(
                text(
                    """UPDATE idempotency_keys SET response_status = :status, response_body = CAST(:body AS jsonb)
                       WHERE user_id = :uid AND key = :key"""
                ),
                {
                    "status": status,
                    "body": None if body is None else json.dumps(body),
                    "uid": self.user_id,
                    "key": self.key,
                },
            )
        return Response(status_code=204) if body is None else JSONResponse(body, status_code=status)

    def _claim(self, conn: Connection) -> Response | None:
        """None: this request is the first for the key and should run. A Response: the stored answer to a repeat."""
        where = {"uid": self.user_id, "key": self.key}
        got_lock = conn.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": lock_name(self.user_id, self.key)},
        ).scalar_one()
        if not got_lock:  # the first request is still running in another transaction
            raise ApiError(409, "request_in_progress", "The first request with this Idempotency-Key is still running.")
        conn.execute(
            text("DELETE FROM idempotency_keys WHERE user_id = :uid AND key = :key AND created_at < now() - :life"),
            {**where, "life": KEY_LIFETIME},
        )
        inserted = conn.execute(
            text(
                """INSERT INTO idempotency_keys (user_id, key, method, path, request_sha256)
                   VALUES (:uid, :key, :method, :path, :sha) ON CONFLICT DO NOTHING RETURNING key"""
            ),
            {**where, "method": self.method, "path": self.path, "sha": self.body_sha256},
        ).one_or_none()
        if inserted is not None:
            return None
        stored = conn.execute(
            text(
                """SELECT method, path, request_sha256, response_status, response_body
                   FROM idempotency_keys WHERE user_id = :uid AND key = :key"""
            ),
            where,
        ).one()
        if (stored.method, stored.path, bytes(stored.request_sha256)) != (self.method, self.path, self.body_sha256):
            raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was used for a different request.")
        if stored.response_status is None:  # cannot happen: the key and its response commit together
            raise ApiError(409, "request_in_progress", "The first request with this Idempotency-Key is still running.")
        return _replay(stored.response_status, stored.response_body)


def _replay(status: int, body: dict[str, Any] | None) -> Response:
    if body is None:
        return Response(status_code=status)
    meta = {**body["meta"], "request_id": log.request_id.get(), "idempotent_replay": True}
    return JSONResponse({**body, "meta": meta}, status_code=status)
