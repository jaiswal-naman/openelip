"""Errors as API.md 1.11 describes them: every failure is the same four-key envelope."""

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import log


class ApiError(Exception):
    """An error the caller should see. code is stable and machine-readable; message is for people."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)  # str(exc) is the code: no message, no value that went into it
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


# What a framework HTTP error (an unknown route, a wrong method) is called in the envelope.
_FRAMEWORK_ERRORS = {
    401: ("unauthenticated", "Sign in to continue."),
    403: ("forbidden", "You may not do this."),
    404: ("not_found", "No such resource."),
    409: ("conflict", "The resource's state does not allow this."),
    413: ("payload_too_large", "The upload is too large."),
    415: ("unsupported_media_type", "That content type is not supported."),
    429: ("rate_limited", "Too many requests. Try again later."),
}
_INVALID_REQUEST = ("invalid_request", "The request is not valid.")
INTERNAL_ERROR = ("internal_error", "Something went wrong on our side.")


def error_response(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body = {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details or {}},
        "meta": {"request_id": log.request_id.get()},
    }
    return JSONResponse(body, status_code=status)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return error_response(error.status, error.code, error.message, error.details)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    status = cast(StarletteHTTPException, exc).status_code
    code, message = _FRAMEWORK_ERRORS.get(status) or (INTERNAL_ERROR if status >= 500 else _INVALID_REQUEST)
    return error_response(status, code, message)


async def _validation_failed(request: Request, exc: Exception) -> JSONResponse:
    problems = cast(RequestValidationError, exc).errors()
    if any(problem["type"] == "json_invalid" for problem in problems):
        return error_response(400, "invalid_request", "The request body is not valid JSON.")
    # loc[0] says where the field came from (body, query, path); the rest is the field's name. The input is not echoed.
    fields = [{"field": ".".join(str(part) for part in p["loc"][1:]), "issue": p["msg"]} for p in problems]
    return error_response(422, "validation_failed", "One or more fields are invalid.", {"fields": fields})


def register(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_failed)
