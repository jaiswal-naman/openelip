"""The only way the application logs (ARCHITECTURE 9.1 and 9.2; requirement PLT-07).

One JSON line per event on standard output. A line carries ids, enumerations, counts, durations and datetimes. A string
is accepted only under an allow-listed identifier key, so resume, chat, contact or job-description text cannot reach a
log by accident. Anything else raises in tests (APP_ENV=test) and is dropped, with redacted=true on the line, elsewhere.
An exception is logged by its class name, never by its message.
"""

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

# Keys under which a string is an identifier the code chose, never text a person wrote.
IDENTIFIER_KEYS = frozenset(
    {"route", "task", "status", "error_code", "kind", "queue", "outcome", "provider", "model", "method"}
)

# Set by the API's request middleware to an id the code generated; every line logged during a request carries it.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_logger = logging.getLogger("app.event")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

_REFUSED = object()


def _clean(key: str, value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, str) and key in IDENTIFIER_KEYS:
        return value
    return _REFUSED


def event(name: str, /, *, level: int = logging.INFO, **fields: Any) -> None:
    """Write one event line. name is "module.what_happened", for example "queue.job"."""
    line: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": logging.getLevelName(level).lower(),
        "event": name,
    }
    if (current := request_id.get()) is not None:
        line["request_id"] = current
    redacted = False
    for key, value in fields.items():
        clean = _clean(key, value)
        if clean is not _REFUSED:
            line[key] = clean
        elif os.environ.get("APP_ENV") == "test":
            # The key and event name only: the value is exactly what must not be written anywhere.
            raise ValueError(f"log.event {name!r}: text under {key!r}; log an id, a count or an identifier key instead")
        else:
            redacted = True
    if redacted:
        line["redacted"] = True
    _logger.log(level, json.dumps(line, separators=(",", ":")))
