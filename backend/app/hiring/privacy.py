"""The hiring tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

What erasure does (DATA_MODEL 7.2): a person's membership, the introductions addressed to them, their shortlist entries
and their signals go with the user row. An organization and its roles stay when their creator goes, with the creator's
reference set to NULL (the registry calls that set_null: the ownership test refuses "no personal data" for a table that
points at users), and so does an introduction an employer answered, with the answerer set to NULL. A role's requirement
items hold nothing about a person.

The export holds the person's own rows. The people who invited, answered or signalled are other people's identifiers,
so those columns are left out of it.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust.privacy import ModuleExport, register

ROWS = {
    "org_members": """SELECT to_jsonb(m) - 'invited_by' FROM org_members m
                      WHERE m.user_id = :u ORDER BY m.created_at, m.id""",
    "introductions": """SELECT to_jsonb(i) - 'employer_answered_by' FROM introductions i
                        WHERE i.user_id = :u ORDER BY i.asked_at, i.id""",
    "shortlist_entries": """SELECT to_jsonb(s) FROM shortlist_entries s
                            WHERE s.user_id = :u ORDER BY s.created_at, s.id""",
    "interest_signals": """SELECT to_jsonb(g) - 'sent_by' FROM interest_signals g
                           WHERE g.user_id = :u ORDER BY g.created_at, g.id""",
}


def export_hiring(conn: Connection, user_id: UUID) -> ModuleExport:
    """The person's memberships, the introductions addressed to them (with the preview they agreed to share and the
    employer's answer), their shortlist entries and the signals about them, as JSON-ready rows. Rows are selected by the
    person's id whatever role runs this, so the worker exports what the API would show them."""
    return ModuleExport(
        data={table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()}
    )


register(
    "hiring",
    {
        **dict.fromkeys(ROWS, "cascade"),
        "organizations": "set_null",
        "roles": "set_null",
        "role_requirements": "no_personal_data",
    },
    exporter=export_hiring,
)
