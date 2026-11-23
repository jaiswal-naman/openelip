"""The tailoring tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

Both tables are erased by cascade from users (DATA_MODEL 7.2). The export holds every row of each as stored, discarded
edits included: they are rows about the person. Plan 05-05 adds the tailored resumes rendered from approved edits.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "tailored_resumes": "SELECT to_jsonb(t) FROM tailored_resumes t WHERE t.user_id = :u ORDER BY t.created_at, t.id",
    "resume_edits": """SELECT to_jsonb(e) FROM resume_edits e WHERE e.user_id = :u
                        ORDER BY e.tailored_resume_id, e.ordinal""",
}


def export_tailor(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's tailored resumes and their edits as JSON-ready rows. Rows are selected by the person's id whatever
    role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("tailor", dict.fromkeys(ROWS, "cascade"), exporter=export_tailor)
