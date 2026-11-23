"""The signed-in person for a request (ARCHITECTURE 6.2), and the sealing of sign-in codes (ARCHITECTURE 7.3).

The session token is a random string sent as a cookie; only its SHA-256 is stored. A request costs one indexed lookup
and one check of the idle limit. Row-level security hides `sessions` and `users` from a request that has no user id yet,
so the lookup goes through the SECURITY DEFINER function resolve_session (DATA_MODEL 5.2).
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet, MultiFernet
from fastapi import Request
from sqlalchemy import Connection, text

from . import config, db
from .errors import ApiError

COOKIE_NAME = "app_session"
ABSOLUTE_LIFETIME = timedelta(days=90)  # assumption (ARCHITECTURE 6.2)
IDLE_LIMIT = timedelta(days=30)  # assumption
TOUCH_INTERVAL = timedelta(minutes=1)  # last_seen_at is written at most this often


@dataclass(frozen=True)
class Principal:
    user_id: UUID


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def act_as(conn: Connection, user_id: UUID) -> None:
    """Make row-level security see this person for the rest of the transaction (db.transaction does it at the start,
    but the sign-in path only learns the user id part-way through)."""
    conn.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)})


def start(conn: Connection, user_id: UUID) -> str:
    """Open a session for a verified person and return the cookie value. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        text(
            """INSERT INTO sessions (user_id, token_sha256, expires_at, last_seen_at)
               VALUES (:uid, :hash, now() + :lifetime, now())"""
        ),
        {"uid": user_id, "hash": token_hash(token), "lifetime": ABSOLUTE_LIFETIME},
    )
    return token


def revoke(conn: Connection, token: str) -> None:
    conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE token_sha256 = :hash AND revoked_at IS NULL"),
        {"hash": token_hash(token)},
    )


def cookie_options() -> dict[str, Any]:
    """The cookie's attributes (API.md 1.2). Secure whenever the public address is https."""
    return {
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": urlsplit(config.get().app_base_url).scheme == "https",
    }


def current_user(request: Request) -> Principal:
    """FastAPI dependency for a route that needs a signed-in person (401 unauthenticated otherwise)."""
    token = request.cookies.get(COOKIE_NAME)
    user_id = _live_session_user(token_hash(token)) if token else None
    if user_id is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    return Principal(user_id)


def _live_session_user(digest: bytes) -> UUID | None:
    """The owner of an unrevoked, unexpired session of an active account, or None. Touches last_seen_at."""
    with db.transaction() as conn:
        user_id: UUID | None = conn.execute(text("SELECT resolve_session(:hash)"), {"hash": digest}).scalar_one()
        if user_id is None:
            return None
        act_as(conn, user_id)
        seen = conn.execute(
            text(
                """SELECT COALESCE(last_seen_at, created_at) > now() - :idle AS is_live,
                          last_seen_at IS NULL OR last_seen_at < now() - :touch AS is_stale
                   FROM sessions WHERE token_sha256 = :hash"""
            ),
            {"hash": digest, "idle": IDLE_LIMIT, "touch": TOUCH_INTERVAL},
        ).one()
        if not seen.is_live:
            return None
        if seen.is_stale:
            conn.execute(text("UPDATE sessions SET last_seen_at = now() WHERE token_sha256 = :hash"), {"hash": digest})
        return user_id


def _fernet() -> MultiFernet:
    """Encrypts with the first key in APP_DATA_KEYS, decrypts with any of them (so a key can be rotated)."""
    return MultiFernet([Fernet(key) for key in config.get().data_keys])


def seal(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def open_sealed(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def seal_bytes(data: bytes) -> bytes:
    """seal() for files (uploaded documents, export archives): same keys, same rotation."""
    return _fernet().encrypt(data)


def open_sealed_bytes(token: bytes) -> bytes:
    return _fernet().decrypt(token)
