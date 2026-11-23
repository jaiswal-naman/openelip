"""The chat tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

Both tables are erased by cascade from users (DATA_MODEL 7.2). Raw chat text stays as the person wrote it: it is visible
only to them, exported with the account and deleted with it (DATA_MODEL 11, issue 15).
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "chat_sessions": "SELECT to_jsonb(s) FROM chat_sessions s WHERE s.user_id = :u ORDER BY s.created_at, s.id",
    "chat_messages": "SELECT to_jsonb(m) FROM chat_messages m WHERE m.user_id = :u ORDER BY m.created_at, m.id",
}


def export_chat(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's sessions and messages as JSON-ready rows. Rows are selected by the person's id whatever role runs
    this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("chat", dict.fromkeys(ROWS, "cascade"), exporter=export_chat)
