"""The match tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

The four tables are erased by cascade from users (DATA_MODEL 7.2); the append-only triggers let that through because
erase_user sets app.maintenance for its own transaction. The export holds every row of each, as the person's own
decisions, evidence and feedback.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "match_runs": "SELECT to_jsonb(r) FROM match_runs r WHERE r.user_id = :u ORDER BY r.started_at, r.id",
    "match_decisions": "SELECT to_jsonb(d) FROM match_decisions d WHERE d.user_id = :u ORDER BY d.created_at, d.id",
    "match_evidence": "SELECT to_jsonb(e) FROM match_evidence e WHERE e.user_id = :u ORDER BY e.id",
    "feedback_events": "SELECT to_jsonb(f) FROM feedback_events f WHERE f.user_id = :u ORDER BY f.created_at, f.id",
}


def export_match(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's runs, decisions, evidence and feedback as JSON-ready rows. Rows are selected by the person's id
    whatever role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("match", dict.fromkeys(ROWS, "cascade"), exporter=export_match)
