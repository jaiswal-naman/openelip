"""Local mode: one person on their own computer (APP_MODE=local; API.md 2.2 and 2.23).

POST /v1/auth/local, which signs the owner in, is in auth.py beside the other sign-in routes; the rest is here: what the
web needs to know before it shows anything, and the model key the owner pastes into the app instead of .env.

Every one of these routes goes through `local_only`: 404 when the process is not in local mode, and 403 unless the
request was addressed to this computer by name. The second check is what stops DNS rebinding. A web page can make its
own domain resolve to 127.0.0.1 and then call this API as if it were the owner's browser, but the browser still sends
the page's own domain in the Host header, and that is never localhost, 127.0.0.1 or [::1]. The browser reaches the API
only through the proxy, which passes the Host header on unchanged (Caddyfile).
"""

import re
from dataclasses import asdict
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy import Connection, text

from . import config, db, log
from .api import Envelope, envelope
from .errors import ApiError
from .idempotency import Write, idempotent
from .llm import stored_key

# The three names a browser uses for this computer, with an optional port. Anything else, including localhost.evil.com,
# 127.0.0.1.nip.io and a trailing-dot name, is refused: the whole header must match.
LOOPBACK_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?", re.IGNORECASE)


def local_only(request: Request) -> None:
    """FastAPI dependency of every local-mode route."""
    if config.get().app_mode != "local":
        raise ApiError(404, "not_found", "No such resource.")
    hosts = request.headers.getlist("host")  # exactly one Host header, and it names this computer
    if len(hosts) != 1 or LOOPBACK_HOST.fullmatch(hosts[0]) is None:
        log.event("auth.failed", outcome="host_refused")
        raise ApiError(403, "forbidden", "Local mode answers only at localhost, 127.0.0.1 or [::1].")


def server_only() -> None:
    """FastAPI dependency of the emailed-code sign-in routes: local mode has no such sign-in, so they are 404 there.
    Left open, a web page that rebinds its own domain to this computer could make a second account through them, and
    local mode refuses a database that holds two."""
    if config.get().app_mode == "local":
        raise ApiError(404, "not_found", "No such resource.")


router = APIRouter(prefix="/v1", dependencies=[Depends(local_only)])


class ModelState(BaseModel):
    """The model the installation would call, and whether it has a key. Never the key itself."""

    provider: str  # anthropic, openai, openai_compatible (a model server you run) or fake (recorded answers)
    key_set: bool
    key_last4: str | None
    source: Literal["environment", "app"] | None  # where the key in use comes from; the environment wins


class LocalStatus(BaseModel):
    mode: Literal["local"]
    has_owner: bool
    model: ModelState


class ModelKeyRequest(BaseModel):
    provider: Literal["anthropic", "openai"]
    # What a provider shows is visible ASCII with no spaces. Spaces at either end (a pasted newline) are dropped first.
    api_key: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[\x21-\x7e]{8,512}$")]


def _require_owner(conn: Connection, user_id: UUID) -> None:
    """The model key belongs to the owner of this installation; no other account may change it."""
    owner = conn.execute(text("SELECT owner_id FROM local_owner(false)")).scalar_one()
    if owner != user_id:
        raise ApiError(403, "forbidden", "Only the owner of this installation can change the model key.")


@router.get("/local/status", response_model=Envelope[LocalStatus])
def local_status() -> dict[str, Any]:
    with db.transaction() as conn:
        accounts = conn.execute(text("SELECT real_accounts FROM local_owner(false)")).scalar_one()
    model = ModelState(**asdict(stored_key.status(config.get())))
    return envelope(LocalStatus(mode="local", has_owner=accounts > 0, model=model))


@router.put("/settings/model-key", response_model=Envelope[ModelState])
def put_model_key(body: ModelKeyRequest, write: Annotated[Write, Depends(idempotent)]) -> Response:
    """Save the provider and key, sealed. The environment wins: if it already names the provider or holds a key for it,
    nothing saved here would be used, so that is a 409 instead of a silent no-op."""

    def effect(conn: Connection) -> ModelState:
        _require_owner(conn, write.user_id)
        if stored_key.environment_decides(config.get(), body.provider):
            raise ApiError(
                409,
                "conflict",
                "The model is set in the environment (LLM_PROVIDER or an API key in .env). Remove it there to use "
                "a key saved here.",
                {"state": "set_in_environment"},
            )
        stored_key.save(conn, body.provider, body.api_key)
        return ModelState(provider=body.provider, key_set=True, key_last4=body.api_key[-4:], source="app")

    response = write.run(effect)
    stored_key.invalidate()  # after the commit, so the next model call in this process reads the new row
    log.event("settings.model_key_saved", provider=body.provider)
    return response


@router.delete("/settings/model-key", status_code=204)
def delete_model_key(write: Annotated[Write, Depends(idempotent)]) -> Response:
    def effect(conn: Connection) -> None:
        _require_owner(conn, write.user_id)
        stored_key.remove(conn)

    response = write.run(effect, status=204)
    stored_key.invalidate()
    log.event("settings.model_key_removed")
    return response
