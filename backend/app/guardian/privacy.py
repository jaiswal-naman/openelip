"""The Guardian's tables and the checkpoint tables in the deletion registry and in the export (TRU-01, TRU-02;
ARCHITECTURE 8.3 and 8.4; DATA_MODEL 3.10, 3.15 and 7.2).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

The five tables of the Guardian go with the user row by cascade (agent_actions and guardian_checks hold append-only
triggers that erase_user lets through by setting app.maintenance for its own transaction). The three data tables of
schema langgraph have no user column and hold ids only; a trigger on campaigns removes a campaign's thread in the same
transaction as the campaign row (p7_0702_langgraph), so they are registered as `trigger`. The library's own
checkpoint_migrations holds migration numbers only. The ownership test reads schema langgraph too, so the names here
are schema-qualified.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "agent_runs": "SELECT to_jsonb(r) FROM agent_runs r WHERE r.user_id = :u ORDER BY r.started_at, r.id",
    "agent_actions": "SELECT to_jsonb(a) FROM agent_actions a WHERE a.user_id = :u ORDER BY a.created_at, a.id",
    "approvals": "SELECT to_jsonb(p) FROM approvals p WHERE p.user_id = :u ORDER BY p.requested_at, p.id",
    "guardian_checks": "SELECT to_jsonb(g) FROM guardian_checks g WHERE g.user_id = :u ORDER BY g.checked_at, g.id",
    "autonomy_settings": "SELECT to_jsonb(s) FROM autonomy_settings s WHERE s.user_id = :u ORDER BY s.action_type",
}


def export_guardian(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's recorded steps (outside actions with their payloads included), approvals, Guardian checks and
    autonomy settings as JSON-ready rows. Rows are selected by the person's id whatever role runs this."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register(
    "guardian",
    {
        **dict.fromkeys(ROWS, "cascade"),
        "langgraph.checkpoints": "trigger",
        "langgraph.checkpoint_blobs": "trigger",
        "langgraph.checkpoint_writes": "trigger",
        "langgraph.checkpoint_migrations": "no_personal_data",
    },
    exporter=export_guardian,
)
