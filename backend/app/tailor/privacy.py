"""The tailoring tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

Both tables are erased by cascade from users (DATA_MODEL 7.2). The export holds every row of each as stored, discarded
edits included: they are rows about the person, and the archive is their own data (05-01 deviation 10). The rendered
resumes are added as files (DATA_MODEL 7.3): one DOCX for each tailored resume that has an approved edit, drawn from
approved edits only, exactly as the download draws it.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

from . import service
from .export import render_docx

ROWS = {
    "tailored_resumes": "SELECT to_jsonb(t) FROM tailored_resumes t WHERE t.user_id = :u ORDER BY t.created_at, t.id",
    "resume_edits": """SELECT to_jsonb(e) FROM resume_edits e WHERE e.user_id = :u
                        ORDER BY e.tailored_resume_id, e.ordinal""",
}
WITH_APPROVED_EDIT = text(
    """SELECT t.id FROM tailored_resumes t
        WHERE t.user_id = :u AND t.status = 'ready'
          AND EXISTS (SELECT 1 FROM resume_edits e WHERE e.tailored_resume_id = t.id AND e.verifier_status = 'passed'
                         AND e.decision = 'approved')
        ORDER BY t.created_at, t.id"""
)


def export_tailor(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's tailored resumes and their edits as JSON-ready rows, and each approved resume as a DOCX. Rows are
    selected by the person's id whatever role runs this, so the worker exports what the API would show them."""
    files = {
        f"files/tailored-resumes/tailored-resume-{found}.docx": render_docx(service.export_doc(conn, user_id, found))
        for found in conn.execute(WITH_APPROVED_EDIT, {"u": user_id}).scalars()
    }
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()},
        files=files,
    )


register("tailor", dict.fromkeys(ROWS, "cascade"), exporter=export_tailor)
