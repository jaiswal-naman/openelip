"""The platform's and the trust module's own tables in the deletion registry (DATA_MODEL 7.2), and trust's export.

app.platform is the lowest layer and may not import app.trust, so the tables it owns are registered here on its behalf,
as are the two eval tables Phase 1 made (the evals module registers nothing yet). Only tables that exist at this
revision are listed. Every module that adds tables registers its own, and the ownership test (plan 03-06) fails on a
table nobody registered. The ingest tables (job_sources, jobs, fetch_runs) belong to Phase 2.

app.trust imports this module, so the registrations are in place wherever the registry is read.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from .privacy import ModuleExport, register


def _rows(conn: Connection, sql: str, user_id: UUID) -> list[Any]:
    return list(conn.execute(text(sql), {"user": user_id}).scalars())


def export_trust(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's consents and audit rows, and the metadata of their data requests, as JSON-ready rows.

    A request's archive is left out: it is the export itself, sealed, and it expires after 7 days. Rows are selected
    by the person's id whatever role runs this, so the worker exports exactly what the API would show them.
    """
    return ModuleExport(
        data={
            "consents": _rows(
                conn, "SELECT to_jsonb(c) FROM consents c WHERE c.user_id = :user ORDER BY c.created_at, c.id", user_id
            ),
            "audit_log": _rows(
                conn, "SELECT to_jsonb(a) FROM audit_log a WHERE a.subject_user_id = :user ORDER BY a.id", user_id
            ),
            "data_requests": _rows(
                conn,
                """SELECT to_jsonb(r) - 'export_archive' FROM data_requests r
                   WHERE r.subject_user_id = :user ORDER BY r.created_at, r.id""",
                user_id,
            ),
        }
    )


register(
    "platform",
    {
        "users": "cascade",
        "sessions": "cascade",
        "outbox": "cascade",  # the person's messages
        "queue_jobs": "cascade",  # the person's jobs
        "idempotency_keys": "cascade",
        "login_codes": "by_email",  # no foreign key: erase_user deletes by the account's address
        "llm_calls": "set_null",  # the cost ledger outlives the person, with no reference to them
        "model_registry": "no_personal_data",
    },
)
register(
    "evals",
    {
        "eval_items": "cascade",  # only rows built from this person's data (source_user_id)
        "eval_runs": "no_personal_data",
    },
)
register(
    "trust",
    {
        "consents": "cascade",
        "audit_log": "cascade",  # rows about the person go; the actor reference on others is set to NULL
        "data_requests": "kept",  # the row stays with the bare subject id so a restored database can be erased again
    },
    exporter=export_trust,
)
