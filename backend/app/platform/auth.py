"""Sign-in with an emailed code, sessions and the account (API.md 1.2 and 2.2; ARCHITECTURE 6.1 and 6.2).

The routes and their Pydantic models are the contract: the web types are generated from them. Codes are stored only as
a keyed HMAC and compared in constant time; they reach the relay sealed (sessions.seal) and are never logged.
"""

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from math import ceil
from typing import Annotated, Any, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Connection, text

from . import config, db, errors, log, outbox, sessions
from .api import Envelope, envelope
from .errors import ApiError
from .idempotency import Write, idempotent, json_only
from .llm import registry
from .sessions import Principal, current_user

router = APIRouter(prefix="/v1")

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"  # a plausible address; the database checks for an @ as well

# API.md 1.2 and 1.8; all assumptions.
CODE_LIFETIME = timedelta(minutes=10)
MAX_ATTEMPTS = 5
CODES_PER_EMAIL_PER_HOUR = 5
CODES_PER_ADDRESS_PER_HOUR = 20


class CodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)


class CodeRequested(BaseModel):
    """The same for every address, so the endpoint does not reveal whether an account exists."""

    expires_in_seconds: int


class SessionRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    code: str = Field(pattern=r"^\d{6}$")
    timezone: str | None = None  # an IANA name; used only when this sign-in creates the account

    @field_validator("timezone")
    @classmethod
    def _is_an_iana_name(cls, name: str | None) -> str | None:
        if name is not None:
            try:
                ZoneInfo(name)
            except (KeyError, ValueError, OSError) as error:  # unknown name, malformed name, a directory
                raise ValueError("not an IANA time zone name") from error
        return name


# The consents a person gives in onboarding and settings (API.md 2.3). introduction and agent_access are recorded by the
# action that needs them, so they are not here. The consent rules live in app.trust, which this layer may not import:
# it fills in these types (see `hooks` below).
ConsentPurpose = Literal["terms", "ai_processing", "brief_email", "open_to_introductions"]


class ConsentState(BaseModel):
    """One consent as GET /v1/me reports it. A purpose that was never granted, or was withdrawn, has granted false."""

    purpose: ConsentPurpose
    granted: bool
    granted_at: datetime | None
    text_version: str | None  # the wording the active grant was given under
    providers: list[str] | None = None  # ai_processing only: the providers the wording names today
    current_text_version: str | None = None  # ai_processing only: the version a new grant must carry


class ConsentChange(BaseModel):
    """One consent a person grants or withdraws in PATCH /v1/me. A grant carries the wording version they were shown."""

    purpose: ConsentPurpose
    granted: bool
    text_version: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")

    @model_validator(mode="after")
    def _a_grant_names_its_wording(self) -> Self:
        if self.granted and self.text_version is None:
            raise ValueError("text_version is required to grant a consent")
        return self


BriefFrequency = Literal["daily", "weekly", "paused"]


class BriefSettings(BaseModel):
    """How often the morning brief comes (BRF-03). Whether it also comes by email is the brief_email consent."""

    frequency: BriefFrequency
    weekday: int | None  # a weekly brief's day, ISO 1 (Monday) to 7; set exactly when the frequency is weekly


class BriefSettingsChange(BaseModel):
    frequency: BriefFrequency
    weekday: int | None = Field(default=None, ge=1, le=7)

    @model_validator(mode="after")
    def _a_weekday_goes_with_weekly(self) -> Self:
        if (self.frequency == "weekly") != (self.weekday is not None):  # the users table holds the same rule
            raise ValueError("weekday is required with weekly and not allowed otherwise")
        return self


class Me(BaseModel):
    id: UUID
    email: str
    timezone: str
    created_at: datetime
    consents: list[ConsentState]
    brief: BriefSettings
    capabilities: list[
        str
    ]  # the model tasks that would be called right now (API.md 2.3); empty with no usable provider


class MePatch(BaseModel):
    """PATCH /v1/me: consents, the time zone and the brief schedule, any of them, at least one."""

    consents: list[ConsentChange] | None = Field(default=None, min_length=1, max_length=4)  # one change per purpose
    timezone: str | None = None  # an IANA name
    brief: BriefSettingsChange | None = None

    @field_validator("timezone")
    @classmethod
    def _is_an_iana_name(cls, name: str | None) -> str | None:
        if name is not None:
            try:
                ZoneInfo(name)
            except (KeyError, ValueError, OSError) as error:  # unknown name, malformed name, a directory
                raise ValueError("not an IANA time zone name") from error
        return name

    @model_validator(mode="after")
    def _something_to_change(self) -> Self:
        if self.consents is None and self.timezone is None and self.brief is None:
            raise ValueError("send at least one of consents, timezone or brief")
        return self


# Filled in by app.main when it builds the app. The consent rules live in app.trust, which this layer may not import
# (ARCHITECTURE 3.3), so the account endpoints call them through these two hooks:
#   "consents":        (conn, user_id) -> list[ConsentState], one per purpose
#   "change_consents": (conn, user_id, changes: Sequence[ConsentChange]) -> None; raises ApiError to refuse a change
hooks: dict[str, Callable[..., Any]] = {}


# Codes requested per client address in the last hour: a window of timestamps per address, in this process's memory.
# ponytail: correct with ONE API process (ARCHITECTURE 6.1); with a second, count in PostgreSQL as the email limit does.
_WINDOW_SECONDS = 3600.0
_SWEEP_ABOVE = 10_000  # entries; beyond this, addresses that have gone quiet are dropped
_recent: dict[str, deque[float]] = {}
_recent_lock = threading.Lock()


def normalize_email(address: str) -> str:
    return address.strip().lower()


def _client_key(request: Request) -> str:
    """The client address. Behind the proxy this is the real address only if uvicorn runs with --proxy-headers."""
    host = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))  # one host usually owns a whole /64
    return host


def _address_wait(request: Request) -> int:
    """Count one code request from this client address. 0: go ahead. Otherwise the seconds until it could."""
    key, now = _client_key(request), time.monotonic()
    with _recent_lock:
        if len(_recent) > _SWEEP_ABOVE:
            for quiet in [k for k, seen in _recent.items() if not seen or now - seen[-1] >= _WINDOW_SECONDS]:
                del _recent[quiet]
        window = _recent.setdefault(key, deque())
        while window and now - window[0] >= _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= CODES_PER_ADDRESS_PER_HOUR:
            return max(1, ceil(_WINDOW_SECONDS - (now - window[0])))
        window.append(now)
        return 0


def _rate_limited(seconds: int) -> Response:
    response = errors.error_response(
        429, "rate_limited", "Too many requests. Try again later.", {"retry_after_seconds": seconds}
    )
    response.headers["Retry-After"] = str(seconds)
    return response


def _code_hmac(email: str, code: str) -> bytes:
    """Keyed, because a plain SHA-256 of six digits is reversed in microseconds by anyone who can read the table."""
    return hmac.new(config.get().lookup_key.encode(), f"{email}:{code}".encode(), hashlib.sha256).digest()


def _issue_code(conn: Connection, email: str) -> int:
    """Create a code and queue its email. Returns 0, or the seconds to wait when this email asked too often."""
    # Count and insert must not interleave for one address, or parallel requests would slip past the limit.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"login:{email}"})
    recent = conn.execute(
        text(
            """SELECT count(*) AS n, min(created_at) + :window - now() AS wait
               FROM login_codes WHERE lower(email) = :email AND created_at > now() - :window"""
        ),
        {"email": email, "window": timedelta(hours=1)},
    ).one()
    if recent.n >= CODES_PER_EMAIL_PER_HOUR:
        return max(1, ceil(float(recent.wait.total_seconds())))
    # A new code replaces the previous one (API.md 1.2): mark every older unused code as used.
    conn.execute(
        text("UPDATE login_codes SET consumed_at = now() WHERE lower(email) = :email AND consumed_at IS NULL"),
        {"email": email},
    )
    code = f"{secrets.randbelow(10**6):06d}"
    row = conn.execute(
        text(
            """INSERT INTO login_codes (email, code_hmac, expires_at) VALUES (:email, :hmac, now() + :life)
               RETURNING id, expires_at"""
        ),
        {"email": email, "hmac": _code_hmac(email, code), "life": CODE_LIFETIME},
    ).one()
    outbox.add(
        conn,
        kind="signin",
        dedupe_key=f"signin:{row.id}",
        user_id=None,  # the address may not have an account yet; the relay finds the address through login_codes
        payload={"login_code_id": str(row.id), "code_sealed": sessions.seal(code)},
        expires_at=row.expires_at,
    )
    return 0


def _spend_code(conn: Connection, email: str, code: str) -> int | None:
    """None when the code is right (and is now used up); otherwise how many tries the person has left."""
    # Counting the try and checking the limit are one statement, so parallel guesses cannot exceed MAX_ATTEMPTS.
    row = conn.execute(
        text(
            """UPDATE login_codes SET attempts = attempts + 1
               WHERE id = (SELECT id FROM login_codes
                           WHERE lower(email) = :email AND consumed_at IS NULL AND expires_at > now()
                             AND attempts < :max
                           ORDER BY created_at DESC LIMIT 1)
                 AND consumed_at IS NULL AND attempts < :max
               RETURNING id, code_hmac, attempts"""
        ),
        {"email": email, "max": MAX_ATTEMPTS},
    ).one_or_none()
    if row is None:  # no code, an expired or used one, or one with its five tries spent
        return 0
    if not secrets.compare_digest(bytes(row.code_hmac), _code_hmac(email, code)):
        return MAX_ATTEMPTS - int(row.attempts)
    used = conn.execute(
        text("UPDATE login_codes SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL RETURNING id"),
        {"id": row.id},
    ).one_or_none()
    return None if used is not None else 0  # None here would mean two requests spent the same code at once


def _exchange(email: str, code: str, timezone: str | None) -> tuple[Me, str] | int:
    """Spend the code and open a session. Returns the account and the cookie value, or the tries left.

    A wrong code must be COUNTED, so the failure is returned, not raised: raising inside the transaction would roll the
    counter back.
    """
    with db.transaction() as conn:
        tries_left = _spend_code(conn, email, code)
        if tries_left is not None:
            return tries_left
        user_id = conn.execute(text("SELECT user_for_verified_email(:email)"), {"email": email}).scalar_one()
        sessions.act_as(conn, user_id)
        # now() is the start of this transaction, so created_at = now() is true only for an account made just above.
        account = conn.execute(
            text(
                """SELECT id, email, timezone, created_at, brief_frequency, brief_weekday,
                          status = 'active' AS is_active, created_at = now() AS is_new
                   FROM users WHERE id = :id"""
            ),
            {"id": user_id},
        ).one()
        if not account.is_active:
            raise ApiError(403, "forbidden", "This account cannot sign in.")
        zone = account.timezone
        if account.is_new and timezone:
            conn.execute(text("UPDATE users SET timezone = :zone WHERE id = :id"), {"zone": timezone, "id": user_id})
            zone = timezone
        token = sessions.start(conn, user_id)
        consents = hooks["consents"](conn, user_id)
    me = Me(
        id=account.id,
        email=account.email,
        timezone=zone,
        created_at=account.created_at,
        consents=consents,
        brief=BriefSettings(frequency=account.brief_frequency, weekday=account.brief_weekday),
        capabilities=_capabilities(),
    )
    log.event("auth.signed_in", outcome="new_account" if account.is_new else "existing_account")
    return me, token


@router.post("/auth/codes", status_code=202, response_model=Envelope[CodeRequested], dependencies=[Depends(json_only)])
def request_code(body: CodeRequest, request: Request) -> Response | dict[str, Any]:
    wait = _address_wait(request)
    if wait:
        log.event("auth.failed", outcome="rate_limited_address")
        return _rate_limited(wait)
    with db.transaction() as conn:
        wait = _issue_code(conn, normalize_email(body.email))
    if wait:
        log.event("auth.failed", outcome="rate_limited_email")
        return _rate_limited(wait)
    log.event("auth.code_requested", outcome="issued")
    return envelope(CodeRequested(expires_in_seconds=int(CODE_LIFETIME.total_seconds())))


@router.post("/auth/sessions", status_code=201, response_model=Envelope[Me], dependencies=[Depends(json_only)])
def create_session(body: SessionRequest, response: Response) -> dict[str, Any]:
    outcome = _exchange(normalize_email(body.email), body.code, body.timezone)
    if isinstance(outcome, int):
        log.event("auth.failed", outcome="code_invalid")
        raise ApiError(401, "code_invalid", "That code does not match or has expired.", {"attempts_remaining": outcome})
    me, token = outcome
    max_age = int(sessions.ABSOLUTE_LIFETIME.total_seconds())
    response.set_cookie(sessions.COOKIE_NAME, token, max_age=max_age, **sessions.cookie_options())
    return envelope(me)


@router.delete("/auth/sessions/current", status_code=204)
def end_session(request: Request, write: Annotated[Write, Depends(idempotent)]) -> Response:
    token = request.cookies[sessions.COOKIE_NAME]  # current_user has already shown it is there
    response = write.run(lambda conn: sessions.revoke(conn, token), status=204)
    response.delete_cookie(sessions.COOKIE_NAME, **sessions.cookie_options())
    log.event("auth.signed_out", outcome="revoked")
    return response


def _capabilities() -> list[str]:
    """The model tasks that have a route that would be called right now, in the order models.toml lists them."""
    return [route.task for route in registry.active_routes(config.get())]


def _account(conn: Connection, user_id: UUID) -> Me | None:
    """The person's account as GET /v1/me reports it, or None when their row is gone."""
    row = conn.execute(
        text("SELECT id, email, timezone, created_at, brief_frequency, brief_weekday FROM users WHERE id = :id"),
        {"id": user_id},
    ).one_or_none()
    if row is None:
        return None
    consents: list[ConsentState] = hooks["consents"](conn, user_id)
    return Me(
        id=row.id,
        email=row.email,
        timezone=row.timezone,
        created_at=row.created_at,
        consents=consents,
        brief=BriefSettings(frequency=row.brief_frequency, weekday=row.brief_weekday),
        capabilities=_capabilities(),
    )


def _save_settings(conn: Connection, user_id: UUID, body: MePatch) -> None:
    """The time zone and the brief schedule, whichever of them the request carries. The statement is fixed text."""
    assignments: list[str] = []
    values: dict[str, Any] = {"id": user_id}
    if body.timezone is not None:
        assignments.append("timezone = :zone")
        values["zone"] = body.timezone
    if body.brief is not None:  # both columns, so a weekday never outlives a weekly schedule (the table's own check)
        assignments += ["brief_frequency = CAST(:frequency AS brief_frequency)", "brief_weekday = :weekday"]
        values.update(frequency=body.brief.frequency, weekday=body.brief.weekday)
    if assignments:
        conn.execute(text(f"UPDATE users SET {', '.join(assignments)} WHERE id = :id"), values)


@router.get("/me", response_model=Envelope[Me])
def me(principal: Annotated[Principal, Depends(current_user)]) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        account = _account(conn, principal.user_id)
    if account is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    return envelope(account)


@router.patch("/me", response_model=Envelope[Me])
def update_me(body: MePatch, write: Annotated[Write, Depends(idempotent)]) -> Response:
    """Grant or withdraw consents and change the time zone and the brief schedule. The person is the session's, never
    anything in the body (T-3-01)."""

    def effect(conn: Connection) -> Me:
        if body.consents is not None:
            hooks["change_consents"](conn, write.user_id, body.consents)
        _save_settings(conn, write.user_id, body)
        account = _account(conn, write.user_id)
        if account is None:
            raise ApiError(401, "unauthenticated", "Sign in to continue.")
        return account

    return write.run(effect)
