"""The Guardian source of the candidate's audit log (TRU-03, GRD-02): outside actions that were decided.

An outside action is one that reaches anyone but the candidate (AI_SYSTEM 1). It appears once it is executed, blocked,
rejected, expired, cancelled or failed; one still waiting for a key is on the approvals page instead. A blocked one
carries every failed check's reason code and the sentence `explain` writes for the first. The text is built from codes:
the quoted phrase some reasons carry stays in the recorder and is not copied here. The link opens the recorder entry.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.trust import api as trust

from .explain import explain

SOURCE = "guardian"
STATUSES = ("executed", "blocked", "rejected", "expired", "cancelled", "failed")
LABEL = {"send_follow_up": "follow-up email", "submit_application": "application", "twin_answer": "Twin answer"}
PHRASE = {
    "executed": "went ahead",
    "rejected": "was rejected by you",
    "expired": "expired before you answered",
    "cancelled": "was cancelled",
    "failed": "failed",
}

# No status-change time is stored: an action's time is when it ran, else the later of its creation and its last check.
SQL = """
WITH x AS (
  SELECT a.id, a.status::text AS status, a.action_type::text AS action_type, a.campaign_id, a.organization_id,
         a.outcome_code,
         COALESCE(a.executed_at,
                  GREATEST(a.created_at, (SELECT max(g.checked_at) FROM guardian_checks g
                                           WHERE g.agent_action_id = a.id))) AS occurred
    FROM agent_actions a
   WHERE a.user_id = :u AND a.kind = 'outside_action' AND a.status::text = ANY(:statuses)
     AND (CAST(:campaign AS uuid) IS NULL OR a.campaign_id = CAST(:campaign AS uuid))
)
SELECT x.*,
       (SELECT json_agg(json_build_object('reason', g.reason_code, 'details', g.details) ORDER BY g.id)
          FROM guardian_checks g WHERE g.agent_action_id = x.id AND NOT g.passed) AS failed
  FROM x
 WHERE {before}
 ORDER BY x.occurred DESC, ('guardian:' || lpad(x.id::text, :width, '0')) COLLATE "C" DESC
 LIMIT :limit"""


def _sentence(code: str, details: dict[str, Any]) -> str | None:
    """`explain` for a code, with its parameters taken from codes only; None when the code has no template."""
    what = details.get("what")
    names = [what] if isinstance(what, str) else what if isinstance(what, list) else []
    detail = ", ".join(n for n in names if trust.audit_code(n)) or "restricted details"
    try:
        return explain(code, quote="", detail=detail).replace(': ""', ".")
    except KeyError:
        return None


def read(conn: Connection, user_id: UUID, f: trust.AuditFilter) -> list[trust.AuditEntry]:
    if f.agent_token_id is not None:  # the actions of a person's own agents carry no outside token
        return []
    before, params = trust.before_sql("x.occurred", "'guardian:' || lpad(x.id::text, :width, '0')", f)
    rows = conn.execute(
        text(SQL.format(before=before)),
        {
            "u": user_id,
            "statuses": list(STATUSES),
            "campaign": f.campaign_id,
            "limit": f.limit,
            "width": trust.ID_WIDTH,
            **params,
        },
    ).all()
    entries = []
    for row in rows:
        label = LABEL.get(row.action_type, "action")
        failed = [c for c in (row.failed or []) if trust.audit_code(c["reason"])]
        reasons = [c["reason"] for c in failed]
        if row.status == "blocked":
            kind = "guardian.action_blocked"
            first = _sentence(reasons[0], failed[0]["details"] or {}) if reasons else None
            if first is None and row.outcome_code:  # blocked by the policy engine before any check ran
                reasons = [c for c in [trust.audit_code(row.outcome_code)] if c]
                first = _sentence(reasons[0], {}) if reasons else None
            summary = f"The {label} was blocked. {first or ''}".strip()
        else:
            kind = f"guardian.action_{row.status}"
            summary = f"The {label} {PHRASE[row.status]}."
            reasons = [c for c in [trust.audit_code(row.outcome_code)] if c] if row.status == "failed" else []
        entries.append(
            trust.AuditEntry(
                id=trust.entry_id(SOURCE, row.id),
                occurred_at=row.occurred,
                source=SOURCE,
                kind=kind,
                actor_type="agent",
                summary=summary,
                outcome=row.status,
                reason_codes=reasons,
                campaign_id=row.campaign_id,
                organization={"id": str(row.organization_id), "name": None} if row.organization_id else None,
                link={"kind": "recorder", "href": f"/control/recorder?entry={row.id}"},
            )
        )
    return entries


trust.audit_source(SOURCE, read)
