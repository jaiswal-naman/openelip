"""The tailoring tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

Both tables are erased by cascade from users (DATA_MODEL 7.2). The export holds every row of each as stored, discarded
edits included: they are rows about the person, and the archive is their own data (05-01 deviation 10). The rendered
resumes are added as files (DATA_MODEL 7.3): one DOCX for each tailored resume that has an approved edit, drawn from
approved edits only, exactly as the download draws it.

Each edit row carries an `archive_label` (05 fix A1, A6): `discarded` for an edit the verifier threw away (its text is
kept, because it is the person's own data), `not_exported_fact_not_usable` for an approved edit that no export prints
any more because a fact it rests on was retired or corrected, and `kept` otherwise. A README.txt says so.
"""

from typing import Any
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


README = (
    "Tailored resumes\n\n"
    "resume_edits in tailor.json includes the text of edits the verifier DISCARDED (archive_label = 'discarded').\n"
    "They were never shown to you as suggestions and no resume file contains them; they are included because they are rows\n"
    "about you. An approved edit whose supporting fact you have since retired or corrected is kept here with\n"
    "archive_label = 'not_exported_fact_not_usable'; it is left out of every resume file.\n"
)


def label_edit(row: dict[str, Any], usable: set[str]) -> dict[str, Any]:
    """The edit row with its archive label."""
    if row["verifier_status"] == "discarded":
        label = "discarded"
    elif row["decision"] == "approved" and not service.edit_is_usable(row["supporting_fact_ids"], usable):
        label = "not_exported_fact_not_usable"
    else:
        label = "kept"
    return {**row, "archive_label": label}


def export_tailor(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's tailored resumes and their edits as JSON-ready rows, and each approved resume as a DOCX. Rows are
    selected by the person's id whatever role runs this, so the worker exports what the API would show them."""
    files = {
        f"files/tailored-resumes/tailored-resume-{found}.docx": render_docx(service.export_doc(conn, user_id, found))
        for found in conn.execute(WITH_APPROVED_EDIT, {"u": user_id}).scalars()
    }
    data = {table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    if data["resume_edits"]:
        usable = service.usable_fact_ids(conn, user_id)
        data["resume_edits"] = [label_edit(row, usable) for row in data["resume_edits"]]
        files["files/tailored-resumes/README.txt"] = README.encode()
    return ModuleExport(data=data, files=files)


register("tailor", dict.fromkeys(ROWS, "cascade"), exporter=export_tailor)
