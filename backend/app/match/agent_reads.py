"""What an outside agent holding a candidate's token reads of the decision ledger (plan 09-04; NET-01).

Shown decisions only, newest first, and nothing else. These are not the readers behind /v1/decisions: those re-read each
decision against the person's hard constraints as they are now, and a token has no read on the constraints table, so
here a decision is what its run stored (`outcome = 'shown'`, which the token's policy already limits it to). The
candidate's own app still re-checks on every read. Each query also names the candidate, so another person's decision is
simply not found.

The verdict counts and the quotes come from match_evidence and profile_facts, which the token reads only with
`explanations:read` (agent_explanations): without that scope the counts are None, not zeros, because a count of zero
would claim something nobody checked. A cited fact the candidate has since corrected is no longer readable by a token,
so its quote is left out.
"""

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from .queries import NOT_A_ROLE_RUN

VERDICTS = ("met", "partly_met", "not_met", "no_evidence")


class BadCursor(ValueError):
    """A cursor this list did not issue."""


@dataclass(frozen=True)
class AgentJob:
    id: int
    title: str
    company_name: str
    country: str | None
    city: str | None
    work_arrangements: tuple[str, ...]
    apply_url: str | None
    status: str
    last_verified_at: datetime | None


@dataclass(frozen=True)
class AgentMatch:
    decision_id: UUID
    local_date: date
    job: AgentJob
    verdict_counts: dict[str, int] | None  # None without explanations:read
    is_saved: bool


@dataclass(frozen=True)
class AgentRequirement:
    requirement_text: str
    importance: str
    verdict: str
    job_quote: str
    record_quotes: tuple[str, ...]


@dataclass(frozen=True)
class AgentExplanation:
    decision_id: UUID
    job: AgentJob
    reason_codes: tuple[str, ...]
    unverified: tuple[str, ...]
    change_note: str | None
    evidence: tuple[AgentRequirement, ...]


_JOB = """j.id AS job_id, j.title, j.company_name, j.country, j.city,
       CAST(j.work_arrangements AS text[]) AS work_arrangements, j.apply_url, j.status::text AS job_status,
       j.last_verified_at"""
_FROM = "match_decisions d JOIN jobs j ON j.id = d.job_id"
_SHOWN = f"d.user_id = :u AND d.outcome = 'shown' AND {NOT_A_ROLE_RUN}"


def _job(row: Any) -> AgentJob:
    return AgentJob(
        id=row.job_id,
        title=row.title,
        company_name=row.company_name,
        country=row.country,
        city=row.city,
        work_arrangements=tuple(row.work_arrangements or ()),
        apply_url=row.apply_url,
        status="open" if row.job_status == "open" else "closed",
        last_verified_at=row.last_verified_at,
    )


def _encode(day: date, at: datetime, ident: UUID) -> str:
    raw = json.dumps([day.isoformat(), at.isoformat(), str(ident)])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[date, datetime, UUID]:
    try:
        day, at, ident = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return date.fromisoformat(day), datetime.fromisoformat(at), UUID(ident)
    except (ValueError, TypeError):
        raise BadCursor("not a cursor of this list") from None


def _like(words: str) -> str:
    """A LIKE pattern that finds the words literally: the wildcard characters of the caller's text are escaped."""
    escaped = words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(
    conn: Connection,
    user_id: UUID,
    *,
    query: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    saved_only: bool = False,
    limit: int = 10,
    cursor: str | None = None,
    with_verdicts: bool = False,
) -> tuple[list[AgentMatch], str | None]:
    """The candidate's shown decisions, newest local date first, one page, and the cursor of the next page."""
    where = [_SHOWN]
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    if query:
        where.append("(j.title ILIKE :q ESCAPE '\\' OR j.company_name ILIKE :q ESCAPE '\\')")
        params["q"] = _like(query)
    if date_from:
        where.append("d.local_date >= :d0")
        params["d0"] = date_from
    if date_to:
        where.append("d.local_date <= :d1")
        params["d1"] = date_to
    if saved_only:
        where.append(f"({_SAVED}) = true")
    if cursor:
        params["c_day"], params["c_at"], params["c_id"] = _decode(cursor)
        where.append("(d.local_date, d.created_at, d.id) < (:c_day, :c_at, :c_id)")
    rows = conn.execute(
        text(
            f"""SELECT d.id AS decision_id, d.local_date, d.created_at, ({_SAVED}) AS is_saved, {_JOB}
                  FROM {_FROM} WHERE {" AND ".join(where)}
                 ORDER BY d.local_date DESC, d.created_at DESC, d.id DESC LIMIT :n"""
        ),
        params,
    ).all()
    page = rows[:limit]
    counts: dict[UUID, dict[str, int]] = {}
    if with_verdicts and page:
        found = conn.execute(
            text(
                """SELECT e.match_decision_id AS id, e.verdict::text AS verdict, count(*) AS n
                     FROM match_evidence e WHERE e.user_id = :u AND e.match_decision_id = ANY(:ids)
                    GROUP BY 1, 2"""
            ),
            {"u": user_id, "ids": [row.decision_id for row in page]},
        )
        for item in found:
            counts.setdefault(item.id, dict.fromkeys(VERDICTS, 0))[item.verdict] = item.n
    matches = [
        AgentMatch(
            row.decision_id,
            row.local_date,
            _job(row),
            counts.get(row.decision_id, dict.fromkeys(VERDICTS, 0)) if with_verdicts else None,
            bool(row.is_saved),
        )
        for row in page
    ]
    next_cursor = _encode(page[-1].local_date, page[-1].created_at, page[-1].decision_id) if len(rows) > limit else None
    return matches, next_cursor


# The latest feedback event of the job decides (a save after a dismissal takes it back), as the ledger does.
_SAVED = """COALESCE((SELECT f.kind::text = 'save' FROM feedback_events f
                       WHERE f.user_id = d.user_id AND f.job_id = d.job_id
                       ORDER BY f.created_at DESC, f.id DESC LIMIT 1), false)"""


def explain(conn: Connection, user_id: UUID, decision_id: UUID) -> AgentExplanation | None:
    """One shown decision of the candidate with its requirement table; None when it is withheld, filtered, a role run's,
    another person's or unknown."""
    row = conn.execute(
        text(
            f"""SELECT d.id AS decision_id, CAST(d.reason_codes AS text[]) AS reason_codes,
                       CAST(d.unverified AS text[]) AS unverified, d.change_note, {_JOB}
                  FROM {_FROM} WHERE d.id = :id AND {_SHOWN}"""
        ),
        {"id": decision_id, "u": user_id},
    ).one_or_none()
    if row is None:
        return None
    evidence = conn.execute(
        text(
            """SELECT r.text, r.importance::text AS importance, e.verdict::text AS verdict, e.fact_ids
                 FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
                WHERE e.user_id = :u AND e.match_decision_id = :id ORDER BY r.ordinal"""
        ),
        {"u": user_id, "id": decision_id},
    ).all()
    wanted = sorted({fact for item in evidence for fact in item.fact_ids}, key=str)
    quotes: dict[UUID, str] = {}
    if wanted:
        quotes = {
            fact.id: fact.statement
            for fact in conn.execute(
                text("SELECT id, statement FROM profile_facts WHERE user_id = :u AND id = ANY(:ids)"),
                {"u": user_id, "ids": wanted},
            )
        }
    return AgentExplanation(
        decision_id=row.decision_id,
        job=_job(row),
        reason_codes=tuple(row.reason_codes or ()),
        unverified=tuple(row.unverified or ()),
        change_note=row.change_note,
        evidence=tuple(
            AgentRequirement(
                requirement_text=item.text,
                importance=item.importance,
                verdict=item.verdict,
                job_quote=item.text,
                record_quotes=tuple(quotes[f] for f in item.fact_ids if f in quotes),
            )
            for item in evidence
        ),
    )
