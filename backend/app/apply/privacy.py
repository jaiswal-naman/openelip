"""The application tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

All three tables are erased by cascade from users (DATA_MODEL 7.2); the append-only trigger on application_events lets
that through because erase_user sets app.maintenance for its own transaction.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "application_packets": """SELECT to_jsonb(p) FROM application_packets p WHERE p.user_id = :u
                               ORDER BY p.created_at, p.id""",
    "applications": "SELECT to_jsonb(a) FROM applications a WHERE a.user_id = :u ORDER BY a.created_at, a.id",
    "application_events": """SELECT to_jsonb(e) FROM application_events e WHERE e.user_id = :u
                              ORDER BY e.application_id, e.occurred_at, e.id""",
}


def export_apply(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's packets, applications and status history as JSON-ready rows. Rows are selected by the person's id
    whatever role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("apply", dict.fromkeys(ROWS, "cascade"), exporter=export_apply)
