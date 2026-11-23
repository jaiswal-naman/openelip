"""The model key a person saves in the app instead of in .env (local mode; PUT /v1/settings/model-key).

It is sealed with the data keys (sessions.seal) and held in one row, local_model_key. Both processes that call models
read it: the API process on its own login and the worker on its login, so each process says which one it is (use_login).
The row is read at most once every CACHE_SECONDS, so a model call does not cost a query, and a key saved in the API
shows up in the worker within that time.

The environment wins. LLM_PROVIDER, ANTHROPIC_API_KEY and OPENAI_API_KEY, when set, are used as they always were; the
saved key only fills in what they leave out (apply). Nothing here returns or logs the key, only its last four
characters.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Literal

from cryptography.fernet import InvalidToken
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

from .. import db, log, sessions
from ..config import Role, Settings

Provider = Literal["anthropic", "openai"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai")
CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class StoredKey:
    provider: Provider
    api_key: str


_login: Role = "worker"  # the worker and the operator commands run in the worker container; the API says "api"
_lock = threading.Lock()
_cached: tuple[float, StoredKey | None] | None = None  # (when it was read, what was found)


def use_login(role: Role) -> None:
    """Name the database login this process holds. The API process calls it with "api" when it is built."""
    global _login
    _login = role
    invalidate()


def invalidate() -> None:
    """Forget what was read, so the next model call reads the row again (after a save or a delete)."""
    global _cached
    with _lock:
        _cached = None


def _read() -> StoredKey | None:
    try:
        with db.transaction(role=_login) as conn:
            row = conn.execute(text("SELECT provider, api_key_sealed FROM local_model_key")).one_or_none()
        if row is None:
            return None
        return StoredKey(row.provider, sessions.open_sealed(row.api_key_sealed))
    except (SQLAlchemyError, InvalidToken) as error:
        # A saved key that cannot be read is the same as no key: the person is asked for it again. Only the class of the
        # failure is logged, never its message.
        log.event("model_key.unreadable", level=logging.WARNING, error=error)
        return None


def current() -> StoredKey | None:
    """The saved key, from the cache when it is younger than CACHE_SECONDS."""
    global _cached
    now = time.monotonic()
    with _lock:
        if _cached is not None and now - _cached[0] < CACHE_SECONDS:
            return _cached[1]
    found = _read()
    with _lock:
        _cached = (now, found)
    return found


def _environment_names_a_model(settings: Settings) -> bool:
    return settings.llm_provider_is_set or bool(settings.anthropic_api_key or settings.openai_api_key)


def environment_decides(settings: Settings, provider: Provider) -> bool:
    """Whether the environment already settles the model for this provider: a key saved in the app would go unused."""
    if not _environment_names_a_model(settings):
        return False
    env_key = settings.anthropic_api_key if provider == "anthropic" else settings.openai_api_key
    return settings.llm_provider != provider or env_key is not None


def apply(settings: Settings) -> Settings:
    """The settings a model call runs with: the environment's, plus the saved key where the environment left a gap."""
    if settings.app_mode != "local":
        return settings
    saved = current()
    if saved is None:
        return settings
    provider = settings.llm_provider
    if not _environment_names_a_model(settings):
        provider = saved.provider  # the environment said nothing about the model: the person's choice stands
    fits = provider == saved.provider
    return replace(
        settings,
        llm_provider=provider,
        anthropic_api_key=settings.anthropic_api_key or (saved.api_key if fits and provider == "anthropic" else None),
        openai_api_key=settings.openai_api_key or (saved.api_key if fits and provider == "openai" else None),
    )


@dataclass(frozen=True)
class ModelStatus:
    provider: str
    key_set: bool
    key_last4: str | None
    source: Literal["environment", "app"] | None


def _key_of(settings: Settings) -> str | None:
    """The key the settings' provider would send, if it has one."""
    return {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "openai_compatible": settings.openai_api_key,
    }.get(settings.llm_provider)


def status(settings: Settings) -> ModelStatus:
    """What GET /v1/local/status says about the model: the provider in use and whether it has a key (never the key)."""
    effective = apply(settings)
    key = _key_of(effective)
    if key is not None:
        return ModelStatus(effective.llm_provider, True, key[-4:], "environment" if _key_of(settings) else "app")
    # A local model server (Ollama and the like) needs no key. The fake needs none either, but it is no model to set up.
    ready = effective.llm_provider == "openai_compatible" and effective.llm_base_url is not None
    return ModelStatus(effective.llm_provider, ready, None, None)


def save(conn: Connection, provider: Provider, api_key: str) -> None:
    """Seal and store the key (one row; a new key replaces the old). The caller's transaction commits it."""
    conn.execute(
        text(
            """INSERT INTO local_model_key (provider, api_key_sealed, key_last4)
               VALUES (:provider, :sealed, :last4)
               ON CONFLICT (singleton) DO UPDATE SET provider = EXCLUDED.provider,
                 api_key_sealed = EXCLUDED.api_key_sealed, key_last4 = EXCLUDED.key_last4, updated_at = now()"""
        ),
        {"provider": provider, "sealed": sessions.seal(api_key), "last4": api_key[-4:]},
    )


def remove(conn: Connection) -> None:
    conn.execute(text("DELETE FROM local_model_key"))
