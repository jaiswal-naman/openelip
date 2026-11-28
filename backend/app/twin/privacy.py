"""The Twin tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

What erasure does (DATA_MODEL 7.2): the candidate's topic permissions, their Twin sessions and every message in them
(the employer's questions, the Twin's answers, their own replies) go with the user row by cascade. A session keeps
standing when the employer member who opened it is erased: opened_by (and agent_token_id) is set to NULL by the foreign
key. The checkpoint thread `twin:{question_id}` of each question goes in the same transaction, through the trigger of
revision p9_0901_twin_agentnet (the langgraph tables themselves are registered once, by the guardian module).

The export holds the person's own rows. The employer member and the token that opened a session are other people's
identifiers, so those two columns are left out of it.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "twin_scopes": "SELECT to_jsonb(s) FROM twin_scopes s WHERE s.user_id = :u ORDER BY s.topic",
    "twin_sessions": """SELECT to_jsonb(s) - 'opened_by' - 'agent_token_id' FROM twin_sessions s
                        WHERE s.user_id = :u ORDER BY s.created_at, s.id""",
    "twin_messages": "SELECT to_jsonb(m) FROM twin_messages m WHERE m.user_id = :u ORDER BY m.created_at, m.id",
}


def export_twin(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's topic permissions, Twin sessions and every message in them as JSON-ready rows. Rows are selected by
    the person's id whatever role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("twin", dict.fromkeys(ROWS, "cascade"), exporter=export_twin)
