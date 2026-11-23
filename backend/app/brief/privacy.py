"""The brief tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

Both tables are erased by cascade from users (DATA_MODEL 7.2). The brief's email is a row of the platform's outbox,
which the platform registers and exports.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "briefs": "SELECT to_jsonb(b) FROM briefs b WHERE b.user_id = :u ORDER BY b.local_date, b.channel, b.id",
    "brief_items": "SELECT to_jsonb(i) FROM brief_items i WHERE i.user_id = :u ORDER BY i.brief_id, i.position",
}


def export_brief(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's briefs and what each one listed, as JSON-ready rows. Rows are selected by the person's id whatever
    role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("brief", dict.fromkeys(ROWS, "cascade"), exporter=export_brief)
