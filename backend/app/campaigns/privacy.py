"""The campaign tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

All three tables are erased by cascade from users (DATA_MODEL 7.2); deleting a campaign also removes its checkpoint
thread, through the trigger of p7_0702_langgraph, in the same transaction.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "campaigns": "SELECT to_jsonb(c) FROM campaigns c WHERE c.user_id = :u ORDER BY c.created_at, c.id",
    "campaign_steps": "SELECT to_jsonb(s) FROM campaign_steps s WHERE s.user_id = :u ORDER BY s.created_at, s.id",
    # the claims, with their quotes and addresses, are part of the dossier
    "company_dossiers": "SELECT to_jsonb(d) FROM company_dossiers d WHERE d.user_id = :u ORDER BY d.created_at, d.id",
}


def export_campaigns(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's campaigns, steps and dossiers (claims included) as JSON-ready rows. Rows are selected by the
    person's id whatever role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register("campaigns", dict.fromkeys(ROWS, "cascade"), exporter=export_campaigns)
