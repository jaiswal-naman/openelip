"""The frozen share_preview of an ask (API.md 3.12): exactly what an employer receives if the candidate says yes.

    {"on_accept": {"name", "headline", "evidence": [{"requirement", "importance", "verdict", "record_quotes": [...]}],
                   "facts": [{"kind", "statement"}]},
     "on_employer_yes": {"email"}}

It is built once, when the candidate is asked, from the validated evidence of the role run and the candidate's confirmed
facts, and never changed (the database refuses an update). Nothing else about the candidate is in it: no file, document
id, span or other contact field. A key whose value is not known is null, never guessed.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.match import api as match
from app.profile import api as profile

SHARED_KINDS = ("role", "education")  # the confirmed facts an employer sees beside the evidence


def _quote(fact: dict[str, Any]) -> str:
    """What a cited fact says in the candidate's own words: the quote of the document line it came from, or, for a
    fact the candidate wrote by hand, its statement."""
    provenance = fact.get("provenance") or {}
    return str(provenance.get("quote") or fact["statement"])


def _headline(roles: list[dict[str, Any]]) -> str | None:
    """The statement of the role the candidate holds now (no end date), else of the one that started last, else None."""
    if not roles:
        return None
    current = [r for r in roles if not (r.get("attributes") or {}).get("end_date")]
    pool = current or roles
    dated = [r for r in pool if (r.get("attributes") or {}).get("start_date")]
    chosen = max(dated, key=lambda r: r["attributes"]["start_date"]) if dated else pool[-1]
    return str(chosen["statement"])


def build(conn: Connection, user_id: UUID, selection: match.RoleSelection) -> dict[str, Any] | None:
    """The preview for one selected candidate, or None when a fact the evidence cites is no longer usable (it was
    corrected or retired since the run): that candidate is not asked on a stale reading. Runs on the worker connection,
    which can read the account and the record."""
    account = conn.execute(
        text("SELECT display_name, email FROM users WHERE id = :u AND status = 'active'"), {"u": user_id}
    ).one_or_none()
    if account is None:
        return None
    facts = {str(fact["id"]): fact for fact in profile.usable_facts(conn, user_id)}
    evidence = []
    for row in selection.rows:
        if any(str(fact_id) not in facts for fact_id in row.fact_ids):
            return None
        quotes = [_quote(facts[str(fact_id)]) for fact_id in row.fact_ids]
        evidence.append(
            {
                "requirement": row.requirement,
                "importance": row.importance,
                "verdict": row.verdict,
                "record_quotes": quotes,
            }
        )
    shared = [fact for fact in facts.values() if fact["kind"] in SHARED_KINDS]
    return {
        "on_accept": {
            "name": account.display_name,
            "headline": _headline([fact for fact in shared if fact["kind"] == "role"]),
            "evidence": evidence,
            "facts": [{"kind": fact["kind"], "statement": fact["statement"]} for fact in shared],
        },
        "on_employer_yes": {"email": account.email},
    }
