"""Queue handlers of the trust module: trust.export (a person's archive), trust.erase (a person's deletion) and the
daily retention sweep, trust.purge. The worker imports this module, which registers them (as app.ingest.jobs does).

A job carries only the id of its data_requests row. Who it is about comes from that row, never from the payload.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import log
from app.platform.queue import NonRetryable, handler
from app.platform.scheduler import every

from . import deletion, export


def _request_id(payload: dict[str, Any]) -> UUID:
    try:
        return UUID(str(payload["request_id"]))
    except (KeyError, ValueError):
        raise NonRetryable("the job names no request") from None  # no retry can fix a payload


@handler("trust.export")
def export_job(conn: Connection, payload: dict[str, Any]) -> None:
    export.complete_export(conn, _request_id(payload))


@handler("trust.erase")
def erase_job(conn: Connection, payload: dict[str, Any]) -> None:
    deletion.erase(conn, _request_id(payload))


@every("trust.purge", 86400)
def purge(conn: Connection, payload: dict[str, Any]) -> None:
    """DATA_MODEL 7.1: an export archive is cleared 7 days after it was made; purge_expired is where that is said."""
    swept = conn.execute(text("SELECT purge_expired()")).scalar_one()
    log.event("trust.purged", **swept)
