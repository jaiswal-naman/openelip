"""The data export (TRU-01; ARCHITECTURE 8.3; DATA_MODEL 7.3).

A person asks for an export, the trust.export job builds one ZIP and stores it sealed in data_requests.export_archive,
and only that person may download it, for 7 days. The ZIP holds manifest.json, one JSON file per module and the original
uploaded files. Which modules there are is not decided here: a module that holds data about a person registers an
exporter with trust.privacy.register, and the archive is whatever the registered exporters return, so a new module
needs no change in this file.

Every exporter selects by the person's id. The worker role sees every row, so that WHERE is the only fence between one
person's archive and another's; test_export checks it for the exporters registered today.
"""

import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

from pydantic_core import to_json
from sqlalchemy import Connection, text

from app.platform import log, sessions
from app.platform.errors import ApiError
from app.platform.queue import NonRetryable, enqueue

from . import privacy
from .audit import audit_log

FORMAT_VERSION = 1
FORMAT = (
    "ZIP of JSON files, one per data area, with a manifest listing every file and the schema version, "
    "plus the original uploaded documents"
)
DUE = timedelta(days=1)  # when an export should be ready; an open request older than this no longer blocks a new one
KEPT_FOR = timedelta(days=7)  # how long the sealed archive stays (DATA_MODEL 7.1)
PER_DAY = 2  # exports one person may ask for in a day (API.md 1.8; an assumption)

_MODULE_NAME = re.compile(r"[a-z][a-z_]*")
_FILE_PATH = re.compile(r"files/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


def _rows(conn: Connection, sql: str, user_id: UUID) -> list[Any]:
    return list(conn.execute(text(sql), {"u": user_id}).scalars())


# What the platform holds about a person. app.platform may not import app.trust, so these are exported here, beside the
# registration of its tables in platform_tables.py. Two hashes are left out: a session token's hash is a credential and
# a request body's hash says nothing about the person. The embedding, a vector, is written as numbers.
PLATFORM_ROWS = {
    "users": """SELECT to_jsonb(x) || jsonb_build_object('record_embedding', x.record_embedding::real[])
                FROM users x WHERE x.id = :u""",
    "sessions": "SELECT to_jsonb(x) - 'token_sha256' FROM sessions x WHERE x.user_id = :u ORDER BY x.created_at, x.id",
    "outbox": "SELECT to_jsonb(x) FROM outbox x WHERE x.user_id = :u ORDER BY x.created_at, x.id",
    "queue_jobs": "SELECT to_jsonb(x) FROM queue_jobs x WHERE x.user_id = :u ORDER BY x.id",
    "idempotency_keys": """SELECT to_jsonb(x) - 'request_sha256' FROM idempotency_keys x
                           WHERE x.user_id = :u ORDER BY x.created_at, x.key""",
    "llm_calls": "SELECT to_jsonb(x) FROM llm_calls x WHERE x.user_id = :u ORDER BY x.id",
}


def export_platform(conn: Connection, user_id: UUID) -> privacy.ModuleExport:
    """The account row, and the person's sessions, messages, jobs, idempotency records and model-call ledger rows."""
    return privacy.ModuleExport(data={table: _rows(conn, sql, user_id) for table, sql in PLATFORM_ROWS.items()})


def export_evals(conn: Connection, user_id: UUID) -> privacy.ModuleExport:
    """Eval items built from this person's own data (none in Phase 3; the table exists and erasure covers it)."""
    sql = "SELECT to_jsonb(x) FROM eval_items x WHERE x.source_user_id = :u ORDER BY x.id"
    return privacy.ModuleExport(data={"eval_items": _rows(conn, sql, user_id)})


# platform_tables.py registered these two modules' tables without an exporter, and a module takes one: it is added here.
privacy.register("platform", {}, exporter=export_platform)
privacy.register("evals", {}, exporter=export_evals)


@dataclass(frozen=True)
class Built:
    archive: bytes  # the ZIP in the clear: the caller seals it before it is stored
    rows: dict[str, dict[str, int]]  # how many rows each module exported, by table
    files: int  # the original files the modules added (not their JSON, not the manifest)


def _is_safe_path(path: str) -> bool:
    """A relative path under files/ made of plain characters, with no . or .. in it."""
    return _FILE_PATH.fullmatch(path) is not None and not {".", ".."} & set(path.split("/"))


def _add(archive: zipfile.ZipFile, listing: list[dict[str, Any]], module: str, path: str, content: bytes) -> None:
    """One file into the archive and the manifest's list. A path two modules both use is refused, naming the module and
    never the path, which may carry a file name the person chose."""
    if path == "manifest.json" or any(item["path"] == path for item in listing):
        raise ValueError(f"{module}: an archive path is already taken")
    archive.writestr(path, content)
    listing.append({"path": path, "bytes": len(content)})


def build_export(conn: Connection, user_id: UUID) -> Built:
    """Call every registered exporter for the person and pack what they return into one ZIP.

    An exporter's files are already full paths in the archive (files/...); one that could leave the archive is refused.

    ponytail: the whole archive is built in memory. A person's own data is a few files of at most 10 MB each; write to a
    temporary file instead if an archive ever outgrows that.
    """
    exports = {module: export(conn, user_id) for module, export in sorted(privacy.exporters().items())}
    listing: list[dict[str, Any]] = []  # every file in the archive but the manifest
    modules: dict[str, Any] = {}
    rows: dict[str, dict[str, int]] = {}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for module, export in exports.items():
            if not _MODULE_NAME.fullmatch(module):
                raise ValueError("a module name is not a plain identifier")
            _add(archive, listing, module, f"{module}.json", to_json(export.data, indent=2))
            rows[module] = {table: len(value) for table, value in export.data.items() if isinstance(value, list)}
            modules[module] = {"file": f"{module}.json", "rows": rows[module]}
            for path, content in export.files.items():
                if not _is_safe_path(path):
                    raise ValueError(f"{module}: a file path is not a plain path in the files folder")
                _add(archive, listing, module, path, content)
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": datetime.now(timezone.utc),
            "modules": modules,
            "files": listing,
        }
        archive.writestr("manifest.json", to_json(manifest, indent=2))
    return Built(buffer.getvalue(), rows, len(listing) - len(exports))


def request_export(conn: Connection, user_id: UUID) -> UUID:
    """The id of the person's export request: the one still open, or a new one with its job queued.

    Two requests at once make one (a lock on the person). An open request more than a day old no longer counts as open,
    so a job that died for good cannot block a new export for ever. Asking again and again is limited to PER_DAY a day.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"privacy.export:{user_id}"})
    pending: UUID | None = conn.execute(
        text(
            """SELECT id FROM data_requests
               WHERE subject_user_id = :u AND kind = 'export' AND status IN ('open', 'in_progress') AND due_at > now()
               ORDER BY created_at DESC LIMIT 1"""
        ),
        {"u": user_id},
    ).scalar()
    if pending is not None:
        return pending
    recent = conn.execute(
        text(
            """SELECT count(*) AS n,
                      CAST(ceil(extract(epoch FROM min(created_at) + interval '1 day' - now())) AS integer) AS wait
               FROM data_requests
               WHERE subject_user_id = :u AND kind = 'export' AND created_at > now() - interval '1 day'"""
        ),
        {"u": user_id},
    ).one()
    if recent.n >= PER_DAY:
        raise ApiError(
            429,
            "rate_limited",
            f"You can ask for {PER_DAY} exports a day. Try again later, or download the one you already have.",
            {"retry_after_seconds": max(1, recent.wait)},
        )
    request_id: UUID = conn.execute(
        text(
            """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
               VALUES (:u, :u, 'export', now() + :due) RETURNING id"""
        ),
        {"u": user_id, "due": DUE},
    ).scalar_one()
    enqueue(
        conn,
        "trust.export",
        {"request_id": str(request_id)},
        unique_key=f"trust.export:{request_id}",
        user_id=user_id,
    )
    return request_id


def complete_export(conn: Connection, request_id: UUID) -> None:
    """The trust.export job: build the person's archive, seal it and store it with its expiry. Safe to run twice.

    A request that is no longer open is left alone: it finished, or it ended when the person closed their account. An
    account that is no longer active gets no archive, and that request ends as failed.
    """
    request = conn.execute(
        text("SELECT subject_user_id, kind, status FROM data_requests WHERE id = :r FOR UPDATE"), {"r": request_id}
    ).first()
    if request is None or request.kind != "export":
        raise NonRetryable("not an export request")
    if request.status not in ("open", "in_progress"):
        return
    user_id = request.subject_user_id
    if conn.execute(text("SELECT status FROM users WHERE id = :u"), {"u": user_id}).scalar() != "active":
        conn.execute(text("UPDATE data_requests SET status = 'failed' WHERE id = :r"), {"r": request_id})
        log.event("trust.export", data_request_id=request_id, outcome="account_closed")
        return
    built = build_export(conn, user_id)
    results = {"postgres": "exported", "modules": len(built.rows), "files": built.files}
    sealed = sessions.seal_bytes(built.archive)
    conn.execute(
        text(
            """UPDATE data_requests
               SET status = 'completed', completed_at = now(), export_archive = :archive,
                   export_expires_at = now() + :kept, store_results = store_results || CAST(:results AS jsonb)
               WHERE id = :r"""
        ),
        {"archive": sealed, "kept": KEPT_FOR, "results": json.dumps(results), "r": request_id},
    )
    audit_log(
        conn,
        "privacy.export_ready",
        "system",
        user_id,
        {"modules": len(built.rows), "files": built.files},
        entity_table="data_requests",
        entity_id=str(request_id),
    )
    log.event(
        "trust.export", data_request_id=request_id, outcome="completed", modules=len(built.rows), files=built.files
    )
