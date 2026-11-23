"""The agent-token table in the deletion registry and in the export (TRU-01, TRU-02; NET-01).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

A token goes with the user row that issued it (a candidate's own, or an organization member's) by cascade. The export
lists the person's tokens by id, client name, principal, scopes and dates. It never holds `token_sha256`: the secret
cannot be recovered from the hash, and the hash must not travel in an export file.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "agent_tokens": """SELECT to_jsonb(t) - 'token_sha256' FROM agent_tokens t
                       WHERE t.user_id = :u ORDER BY t.created_at, t.id""",
}


def export_agentnet(conn: Connection, user_id: UUID) -> ModuleExport:
    """The tokens the person issued, without their hashes, as JSON-ready rows."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("agentnet", dict.fromkeys(ROWS, "cascade"), exporter=export_agentnet)
