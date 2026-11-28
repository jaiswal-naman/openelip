"""The matching source of the candidate's audit log (TRU-03): one entry per finished candidate match run.

A run's entry says how many matches it showed, withheld and filtered out, with a link to the decisions ledger for that
day where each one has its reasons. The counts come from the run row and one grouped read of its decisions. Role runs
(an organization's) are not the candidate's and are left out, as the ledger leaves them out. The text is built here from
numbers and codes.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from app.trust import api as trust

SOURCE = "matching"
TRIGGER_WORDS = {"nightly": "nightly", "onboarding": "first", "on_demand": "on-request"}

SQL = """
SELECT r.id, r.status::text AS status, r.trigger::text AS trigger, r.local_date, r.finished_at, r.shown_count,
       r.filtered_count, r.error_code,
       count(d.id) FILTER (WHERE d.outcome = 'withheld') AS withheld
  FROM match_runs r
  LEFT JOIN match_decisions d ON d.match_run_id = r.id
 WHERE r.user_id = :u AND r.trigger <> 'role' AND r.status <> 'running' AND r.finished_at IS NOT NULL
   AND {before}
 GROUP BY r.id
 ORDER BY r.finished_at DESC, ('matching:' || r.id::text) COLLATE "C" DESC
 LIMIT :limit"""


def read(conn: Connection, user_id: UUID, f: trust.AuditFilter) -> list[trust.AuditEntry]:
    if f.agent_token_id is not None or f.campaign_id is not None:  # runs belong to no token and no campaign
        return []
    before, params = trust.before_sql("r.finished_at", "'matching:' || r.id::text", f)
    rows = conn.execute(text(SQL.format(before=before)), {"u": user_id, "limit": f.limit, **params}).all()
    entries = []
    for row in rows:
        day = row.local_date.isoformat()
        words = TRIGGER_WORDS.get(row.trigger, "")
        if row.status == "completed":
            kind = "match.run_completed"
            summary = (
                f"Your {words} match run for {day} showed {row.shown_count}, withheld {row.withheld} "
                f"and filtered out {row.filtered_count}."
            ).replace("  ", " ")
            reasons: list[str] = []
        else:
            kind = "match.run_failed"
            reason = trust.audit_code(row.error_code)
            summary = f"Your {words} match run for {day} failed.".replace("  ", " ")
            reasons = [reason] if reason else []
        entries.append(
            trust.AuditEntry(
                id=trust.entry_id(SOURCE, row.id),
                occurred_at=row.finished_at,
                source=SOURCE,
                kind=kind,
                actor_type="system",
                summary=summary,
                outcome=row.status,
                reason_codes=reasons,
                link={"kind": "decisions", "href": f"/v1/decisions?from={day}&to={day}"},
            )
        )
    return entries


trust.audit_source(SOURCE, read)
