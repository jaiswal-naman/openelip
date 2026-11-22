"""Request spans, operation counters and the /metrics endpoint (ARCHITECTURE 9.1 and 9.2; requirement PLT-07).

The content-free rule that holds for logs holds here too: a span attribute or a metric label is an id, a number, a route
template or a name the code chose, never text a person wrote. Strings are accepted only under the identifier keys
app.platform.log allows. An exception is recorded by its class name: the OpenTelemetry defaults (record_exception and
set_status_on_exception) copy str(exc) into the span, and that message can hold a resume, an address or a query.

Spans stay in this process: the manifest has no OTLP exporter package yet, so OTEL_EXPORTER_OTLP_ENDPOINT only produces
a warning at start-up. Add opentelemetry-exporter-otlp and one BatchSpanProcessor on _provider to switch export on.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Response
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config, log

# Our own provider, not the global one: nothing else can attach to it, and a test can swap it for one with an exporter.
_provider = TracerProvider(resource=Resource.create({"service.name": "open-career-agent"}))

REQUESTS = Counter(
    "http_requests", "HTTP requests by method, route template and status code.", ["method", "route", "status"]
)
OPERATIONS = Counter("operations", "Queue jobs and model calls by operation and outcome.", ["operation", "outcome"])

# Any token is a legal HTTP method, so a client could put text in the label. Anything unknown is counted as OTHER.
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _allowed(attributes: dict[str, Any]) -> dict[str, Any]:
    """The attributes a span may carry. Text under any key but log.IDENTIFIER_KEYS raises in tests and is dropped, with
    redacted=true, elsewhere: the same rule as log.event."""
    clean: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            clean[key] = value
        elif isinstance(value, UUID):
            clean[key] = str(value)
        elif isinstance(value, str) and key in log.IDENTIFIER_KEYS:
            clean[key] = value
        elif os.environ.get("APP_ENV") == "test":
            raise ValueError(f"span attribute {key!r}: text is not allowed; use an id, a number or an identifier key")
        else:
            clean["redacted"] = True
    return clean


@contextmanager
def _span(name: str, attributes: dict[str, Any]) -> Iterator[Span]:
    """One span. An exception ends it as an error that names only the exception's class."""
    with _provider.get_tracer("app").start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException as error:
            current.set_attribute("error.type", type(error).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """A span around a queue job or a model call, counted in operations_total by name and outcome (ok or error)."""
    clean = _allowed(attributes)
    outcome = "ok"
    try:
        with _span(name, clean):
            yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        OPERATIONS.labels(name, outcome).inc()


class RequestSpans:
    """Pure ASGI middleware: one span and one count per HTTP request, with the route template and never the raw path."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"] if scope["method"] in KNOWN_METHODS else "OTHER"
        status = 500  # what the client gets if the app raises; api.RequestContext answers with the 500 envelope

        async def remember_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with _span("http.request", {"method": method}) as current:
            try:
                await self.app(scope, receive, remember_status)
            finally:
                route = getattr(scope.get("route"), "path", "unmatched")
                current.set_attribute("route", route)
                current.set_attribute("status_code", status)
                REQUESTS.labels(method, route, str(status)).inc()


def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def instrument(app: FastAPI) -> None:
    """Called by app.main.create_app(): a span and a count per request, and GET /metrics (not in the OpenAPI schema; the
    proxy must not route it to the public side, ARCHITECTURE 9.1)."""
    app.add_middleware(RequestSpans)
    app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)
    if config.get().otel_endpoint:
        log.event("telemetry.export_off", level=logging.WARNING, error_code="exporter_not_installed")
