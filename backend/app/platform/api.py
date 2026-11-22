"""Shared API pieces: the response envelope and request ids (API.md 1.3) and the liveness check (API.md 2.1)."""

import logging
import secrets
from typing import Any, Generic, TypeVar

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import errors, log

T = TypeVar("T")

router = APIRouter()


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any]


class Envelope(BaseModel, Generic[T]):
    """The shape of every JSON response. Routes declare response_model=Envelope[Thing] so the web types match."""

    success: bool
    data: T | None
    error: ErrorBody | None
    meta: dict[str, Any]


def envelope(data: Any, **meta: Any) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None, "meta": {"request_id": log.request_id.get(), **meta}}


@router.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


class RequestContext:
    """Gives every request an id (header X-Request-Id and meta.request_id), and answers an unhandled exception with the
    500 envelope itself. Starlette would re-raise it after answering, and the server's traceback carries the exception
    message, which can hold request content (PLT-07)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = "req_" + secrets.token_hex(6)
        token = log.request_id.set(rid)
        started = False

        async def send_with_id(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            if started:
                raise
            route = getattr(scope.get("route"), "path", "unmatched")  # the template, never the raw path
            log.event("api.unhandled", level=logging.ERROR, route=route, error=exc)
            await errors.error_response(500, *errors.INTERNAL_ERROR)(scope, receive, send_with_id)
        finally:
            log.request_id.reset(token)


def install(app: FastAPI) -> None:
    app.add_middleware(RequestContext)
