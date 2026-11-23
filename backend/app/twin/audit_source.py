"""The Twin source of the candidate's audit log (TRU-03, TWN-04): what their Twin said to employers.

One entry per Twin message about the caller: answered, relayed (their own reply, sent on), declined, or `unknown` (the
holding message: the record did not answer, or each answer waits for them). The text is built here from the kind and
the topic, never from a message body; the organization comes from the introduction and the link opens `/app/twin`.
The Twin acts for no token and no campaign, so those filters return nothing.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.hiring import api as hiring
from app.trust import api as trust

SOURCE = "twin"
PHRASE = {
    "answered": "Your Twin answered an employer's question",
    "relayed": "Your Twin passed your reply to an employer",
    "declined": "Your Twin declined an employer's question",
    "unknown": "Your Twin told an employer it needs to ask you",
}

SQL = """
SELECT m.id, m.created_at, m.answer_kind::text AS answer_kind, m.topic::text AS topic, m.organization_id,
       s.introduction_id
  FROM twin_messages m JOIN twin_sessions s ON s.id = m.session_id
 WHERE m.user_id = :u AND m.author = 'twin' AND {before}
 ORDER BY m.created_at DESC, ('twin:' || m.id::text) COLLATE "C" DESC
 LIMIT :limit"""


def read(conn: Connection, user_id: UUID, f: trust.AuditFilter) -> list[trust.AuditEntry]:
    if f.agent_token_id is not None or f.campaign_id is not None:
        return []
    before, params = trust.before_sql("m.created_at", "'twin:' || m.id::text", f)
    rows = conn.execute(text(SQL.format(before=before)), {"u": user_id, "limit": f.limit, **params}).all()
    labels = hiring.introduction_labels(conn, list({r.introduction_id for r in rows}))
    entries = []
    for row in rows:
        label = labels.get(row.introduction_id)
        topic = trust.audit_code(row.topic)
        about = f" about {topic.replace('_', ' ')}" if topic else ""
        entries.append(
            trust.AuditEntry(
                id=trust.entry_id(SOURCE, row.id),
                occurred_at=row.created_at,
                source=SOURCE,
                kind=f"twin.{row.answer_kind}",
                actor_type="agent",
                summary=f"{PHRASE[row.answer_kind]}{about}.",
                outcome=row.answer_kind,
                organization={
                    "id": str(row.organization_id),
                    "name": None if label is None else label.organization_name,
                },
                link={"kind": "twin", "href": "/app/twin"},
            )
        )
    return entries


trust.audit_source(SOURCE, read)
