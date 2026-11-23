"""The candidate's reads of the decision ledger (plan 04-04; API.md 2.5 and 2.6; MAT-02 to MAT-04, MAT-06, MAT-07).

Plain SQL over match_runs, match_decisions, match_evidence and feedback_events. The routes run it on a connection of the
API role, so row-level security fences every read to the signed-in person; each query also names the person, so someone
else's decision is simply not found (the routes answer 404, never 403). The job's fields come from jobs, job_sources
and job_requirements in the same SQL: the match module may read the job index in SQL, as DATA_MODEL 6.1 does. The
record side of a citation comes from app.profile.api, the only door into the Career Record.

Quotes are stored text, never generated: a requirement's quote is job_requirements.text at its stored span, and a fact's
quote is the text the database checked at its span (or the person's own words when she wrote the fact herself).

Safety is re-checked when a decision is read, not only when a run makes it (invariant 1): a match is listed, opened or
checked only while the person's hard constraints, as they are now, still allow its job, and a decision whose job they
now exclude is reported as filtered wherever the ledger reports it (the list, ?job_id=, the decision page, the filters
and the run's counts). The ledger is append-only, so this is a different reading on the read and edits nothing.
"""

import base64
import binascii
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Row, text

from app.platform.errors import ApiError
from app.profile import api as profile

from .queries import CURRENT_CONSTRAINTS, STILL_ALLOWED, UNVERIFIED_NOW

CHECKER_DISAGREED = "checker_disagreement"  # match_runs.error_code of a run whose two constraint checks differed

# The hard constraints whose field a posting leaves unstated or unclear, by unverified flag (DATA_MODEL 4.1,
# unverified_fields). A shown match can carry these flags; the constraints they name are "not stated", not "satisfied".
UNVERIFIED_KINDS: dict[str, tuple[str, ...]] = {
    "location_not_stated": ("location", "work_authorization"),
    "arrangement_not_stated": ("remote_policy",),
    "arrangement_ambiguous": ("remote_policy",),
    "salary_not_stated": ("salary_floor",),
    "salary_currency_differs": ("salary_floor",),
    "employment_type_not_stated": ("employment_type",),
    "seniority_not_stated": ("seniority",),
}

# The job fields of a match (the Job object of API.md 2.4 and the job in 3.3) and of a ledger row (3.4).
JOB_COLUMNS = """j.id AS job_id, j.title, j.company_name, j.location_raw, j.country, j.city,
       CAST(j.work_arrangements AS text[]) AS work_arrangements, j.employment_type::text AS employment_type,
       j.salary_min, j.salary_max, j.salary_currency, j.apply_url, s.ats::text AS ats, s.token AS board,
       j.status::text AS job_status, j.posted_at, j.last_verified_at, j.content_version"""
JOB_FROM = "jobs j JOIN job_sources s ON s.id = j.source_id"

# A match's `unverified` is read now, from the posting and the constraints as they are (queries.UNVERIFIED_NOW), not the
# flags the run stored: a constraint added or removed since changes which fields are "not stated".
MATCH_COLUMNS = f"""d.id AS decision_id, d.rank_position, d.summary, d.change_note,
       CAST(d.reason_codes AS text[]) AS reason_codes, {UNVERIFIED_NOW} AS unverified, {JOB_COLUMNS}"""
# A ledger row is read now as well (invariant 1): a shown match whose job the person's hard constraints now exclude is
# reported as filtered wherever the ledger reports a decision, so no read path calls a violator shown. HELD_NOW is that
# question for a row of `match_decisions d JOIN jobs j CROSS JOIN c`; OUTCOME_NOW is the outcome the row reads as, which
# the filters of the list ask about, so a held match is in `outcome=filtered` and not in `outcome=shown`.
HELD_NOW = f"(d.outcome = 'shown' AND NOT ({STILL_ALLOWED}))"
OUTCOME_NOW = f"CASE WHEN {HELD_NOW} THEN 'filtered' ELSE d.outcome::text END"
DECISION_COLUMNS = f"""d.id AS decision_id, d.match_run_id, d.local_date, d.outcome::text AS outcome,
       CAST(d.reason_codes AS text[]) AS reason_codes, CAST(d.violated_kinds AS text[]) AS violated_kinds,
       CAST(d.unverified AS text[]) AS unverified, d.rank_position, d.summary, d.change_note, d.created_at,
       CAST(constraint_violations(j, c.cc) AS text[]) AS violated_now, {JOB_COLUMNS}"""

# The run's counts are read now too: a match it showed that the constraints now exclude counts as filtered, not shown.
RUN = text(
    f"""{CURRENT_CONSTRAINTS}
        SELECT r.id, r.record_version, r.shown_count AS shown, r.filtered_count AS filtered, r.error_code,
              (SELECT count(*) FROM match_decisions d
                WHERE d.match_run_id = r.id AND d.outcome = 'withheld') AS withheld,
              (SELECT count(*) FROM match_decisions d JOIN jobs j ON j.id = d.job_id CROSS JOIN c
                WHERE d.match_run_id = r.id AND {HELD_NOW}) AS held_now
         FROM match_runs r
        WHERE r.user_id = :u AND r.local_date = :day AND r.status = 'completed'
        ORDER BY r.finished_at DESC NULLS LAST, r.id DESC LIMIT 1"""
)
EVIDENCE = text(
    """SELECT e.match_decision_id, r.id AS requirement_id, r.text, r.importance::text AS importance,
              lower(r.span) AS span_start, upper(r.span) AS span_end, e.verdict::text AS verdict, e.fact_ids
         FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
        WHERE e.user_id = :u AND e.match_decision_id = ANY(:ids)
        ORDER BY e.match_decision_id, r.ordinal"""
)
GAPS = text(
    """SELECT e.match_decision_id, r.text AS requirement, r.importance::text AS importance, e.verdict::text AS verdict
         FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
        WHERE e.user_id = :u AND e.match_decision_id = ANY(:ids) AND r.importance = 'required'
          AND e.verdict IN ('not_met', 'no_evidence')
        ORDER BY e.match_decision_id, r.ordinal"""
)
LATEST_FEEDBACK = text(
    """SELECT DISTINCT ON (job_id) job_id, kind::text AS kind FROM feedback_events
        WHERE user_id = :u AND job_id = ANY(:jobs) ORDER BY job_id, created_at DESC, id DESC"""
)


def not_found() -> ApiError:
    """The answer for anything that is not the caller's, or does not exist: the same, so ids reveal nothing."""
    return ApiError(404, "not_found", "No such resource.")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timezone_of(conn: Connection, user_id: UUID) -> str:
    return str(conn.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id}).scalar_one())


def today_in(zone: str) -> date:
    """The local date it is now in an IANA time zone: a candidate's "today" is theirs, not the server's (API.md 1.4)."""
    return utc_now().astimezone(ZoneInfo(zone)).date()


def _utc(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(timezone.utc)


def _pay(row: Row[Any]) -> dict[str, Any] | None:
    if row.salary_min is None and row.salary_max is None:
        return None
    return {"min": row.salary_min, "max": row.salary_max, "currency": row.salary_currency}


def _job_brief(row: Row[Any]) -> dict[str, Any]:
    return {
        "id": row.job_id,
        "title": row.title,
        "company_name": row.company_name,
        "pay": _pay(row),
        "status": row.job_status,
        "last_verified_at": _utc(row.last_verified_at),
    }


def _match_job(row: Row[Any]) -> dict[str, Any]:
    arrangements = list(row.work_arrangements)
    return {
        **_job_brief(row),
        "location_raw": row.location_raw,
        "country": row.country,
        "city": row.city,
        "work_arrangements": arrangements,
        "is_remote": True if "remote" in arrangements else (None if not arrangements else False),
        "employment_type": row.employment_type,
        "apply_url": row.apply_url,
        "source": {"ats": row.ats, "board": row.board},
        "posted_at": _utc(row.posted_at),
        "content_version": row.content_version,
    }


# ---- the record side -----------------------------------------------------------------------------------------------


def active_constraints(conn: Connection, user_id: UUID) -> dict[str, dict[str, Any]]:
    """The person's active hard constraints by kind, with their ids and the sentences they confirmed."""
    return {constraint["kind"]: constraint for constraint in profile.get_record(conn, user_id)["constraints"]}


def _constraint_ref(
    kinds: Sequence[str], active: dict[str, dict[str, Any]], decided_at: datetime | None = None
) -> dict[str, Any] | None:
    """The constraint that removed a job: the first kind it broke (enum order). It is named only if it was the person's
    when `decided_at`, the time of the decision, came: one the person has since removed leaves its kind and no id or
    sentence, and so does one replaced after the decision (the active constraint of that kind is another rule, which
    the job may satisfy). The ledger never guesses what the old one said. `decided_at` None is a check made now."""
    if not kinds:
        return None
    found = active.get(kinds[0])
    if found is not None and decided_at is not None and found["created_at"] > decided_at:
        found = None
    return {"id": found["id"] if found else None, "kind": kinds[0], "statement": found["statement"] if found else None}


def constraint_checks(conn: Connection, user_id: UUID, unverified: Sequence[str]) -> list[dict[str, Any]]:
    """Each hard constraint the person has now, "satisfied" or "not_stated". `unverified` is what the posting leaves
    unstated, worked out against those same constraints (UNVERIFIED_NOW), never a flag a run stored: a constraint added
    since is checked like the rest. A job that breaks one is not a match at all (shown_matches and match_by_id leave it
    out), so "violated" is not a state a check can have (FRONTEND_UI 3.4)."""
    unstated = {kind for flag in unverified for kind in UNVERIFIED_KINDS[flag]}
    return [
        {"kind": kind, "statement": c["statement"], "state": "not_stated" if kind in unstated else "satisfied"}
        for kind, c in active_constraints(conn, user_id).items()
    ]


def _citation(fact: dict[str, Any]) -> dict[str, Any]:
    source = fact["provenance"]
    return {
        "fact_id": fact["id"],
        "quote": source["quote"] if source else fact["statement"],
        "source": {"document_id": source["document_id"], "span": source["span"]} if source else None,
        "is_current": fact["status"] == "confirmed",
    }


def evidence_of(conn: Connection, user_id: UUID, decision_ids: Sequence[UUID]) -> dict[UUID, list[dict[str, Any]]]:
    """The requirement table of each decision: every requirement in order with its verdict, the job's quote and span,
    and the cited facts as they were (a fact corrected since shows its original quote and is_current false)."""
    rows = conn.execute(EVIDENCE, {"u": user_id, "ids": list(decision_ids)}).all()
    cited = sorted({fact for row in rows for fact in row.fact_ids}, key=str)
    facts = {f["id"]: f for f in profile.facts_by_ids(conn, user_id, cited)} if cited else {}
    found: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        found[row.match_decision_id].append(
            {
                "requirement": {
                    "id": row.requirement_id,
                    "text": row.text,
                    "importance": row.importance,
                    "job_citation": {"span": {"start": row.span_start, "end": row.span_end}, "quote": row.text},
                },
                "verdict": row.verdict,
                "record_citations": [_citation(facts[str(fact)]) for fact in row.fact_ids],
            }
        )
    return found


def feedback_state(conn: Connection, user_id: UUID, job_ids: Sequence[int]) -> dict[int, tuple[bool, bool]]:
    """(is_saved, is_dismissed) of each job that has an event: the latest event decides, so a save after a dismissal
    takes it back. A job with no event is not in the result."""
    rows = conn.execute(LATEST_FEEDBACK, {"u": user_id, "jobs": list(job_ids)})
    return {row.job_id: (row.kind == "save", row.kind == "dismiss") for row in rows}


# ---- today's matches -----------------------------------------------------------------------------------------------


def run_summary(conn: Connection, user_id: UUID, day: date) -> dict[str, Any] | None:
    """The latest run that finished on a local date, or None when none did. checker says whether the independent
    constraint checker agreed with the filter on every job of the run. The counts are the run's as the person's
    constraints read now: a match the run showed that they now exclude counts as filtered, so the summary never calls
    a violator shown (and never disagrees with the list below it)."""
    row = conn.execute(RUN, {"u": user_id, "day": day}).one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "record_version": row.record_version,
        "shown": row.shown - row.held_now,
        "withheld": row.withheld,
        "filtered": row.filtered + row.held_now,
        "checker": "disagreed" if row.error_code == CHECKER_DISAGREED else "passed",
    }


def _matches(conn: Connection, user_id: UUID, rows: Sequence[Row[Any]]) -> list[dict[str, Any]]:
    evidence = evidence_of(conn, user_id, [row.decision_id for row in rows]) if rows else {}
    state = feedback_state(conn, user_id, [row.job_id for row in rows]) if rows else {}
    matches = []
    for row in rows:
        saved, dismissed = state.get(row.job_id, (False, False))
        matches.append(
            {
                "decision_id": row.decision_id,
                "rank_position": row.rank_position,
                "reason_codes": row.reason_codes,
                "change_note": row.change_note,
                "summary": row.summary,
                "unverified": row.unverified,
                "job": _match_job(row),
                "evidence": evidence.get(row.decision_id, []),
                "feedback": {"is_saved": saved, "is_dismissed": dismissed},
            }
        )
    return matches


def shown_matches(conn: Connection, user_id: UUID, day: date) -> list[dict[str, Any]]:
    """The matches shown on a local date, best first: at most five, because the database allows no more. Only those
    whose job the person's hard constraints still allow: the decision was made under the constraints of the run, and one
    confirmed since holds at once (invariant 1). The stored decision is not touched."""
    sql = f"""{CURRENT_CONSTRAINTS}
              SELECT {MATCH_COLUMNS} FROM match_decisions d JOIN {JOB_FROM} ON j.id = d.job_id CROSS JOIN c
              WHERE d.user_id = :u AND d.local_date = :day AND d.outcome = 'shown' AND {STILL_ALLOWED}
              ORDER BY d.rank_position"""
    return _matches(conn, user_id, conn.execute(text(sql), {"u": user_id, "day": day}).all())


def match_by_id(conn: Connection, user_id: UUID, decision_id: UUID) -> dict[str, Any] | None:
    """One shown match with the posting text and the constraint checks. None for a decision that is not a shown match
    of this person: another person's, one that was withheld or filtered, none at all, or one whose job the person's hard
    constraints now exclude (decision_by_id answers for that one, as filtered)."""
    sql = f"""{CURRENT_CONSTRAINTS}
              SELECT {MATCH_COLUMNS}, j.description
              FROM match_decisions d JOIN {JOB_FROM} ON j.id = d.job_id CROSS JOIN c
              WHERE d.id = :id AND d.user_id = :u AND d.outcome = 'shown' AND {STILL_ALLOWED}"""
    row = conn.execute(text(sql), {"id": decision_id, "u": user_id}).one_or_none()
    if row is None:
        return None
    (match,) = _matches(conn, user_id, [row])
    return {
        **match,
        "description": row.description,
        "constraint_checks": constraint_checks(conn, user_id, row.unverified),
    }


# ---- the decision ledger -------------------------------------------------------------------------------------------


def encode_cursor(*parts: str) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *parsers: Callable[[str], Any]) -> list[Any]:
    """The values a cursor holds, parsed; a cursor this API did not issue is a 400."""
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(parts, list) or len(parts) != len(parsers) or not all(isinstance(p, str) for p in parts):
            raise ValueError
        return [parse(part) for parse, part in zip(parsers, parts, strict=True)]
    except (ValueError, TypeError, binascii.Error) as error:
        raise ApiError(400, "invalid_request", "That cursor is not valid.") from error


def _gaps(conn: Connection, user_id: UUID, decision_ids: Sequence[UUID]) -> dict[UUID, list[dict[str, Any]]]:
    """The required requirements a scored job did not meet or had no evidence for, by decision."""
    found: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    if decision_ids:
        for row in conn.execute(GAPS, {"u": user_id, "ids": list(decision_ids)}):
            found[row.match_decision_id].append(
                {"requirement": row.requirement, "importance": row.importance, "verdict": row.verdict}
            )
    return found


def _held_now(row: Row[Any]) -> bool:
    """Whether a shown match's job is one the person's hard constraints now exclude (HELD_NOW, for a fetched row)."""
    return bool(row.outcome == "shown" and row.violated_now)


def _decisions(conn: Connection, user_id: UUID, rows: Sequence[Row[Any]]) -> list[dict[str, Any]]:
    """Ledger rows as the API shows them, read now. A shown match whose job the person's hard constraints now exclude
    reads as filtered (invariant 1): hard_constraint, the kinds it breaks, the rule that does it (the active one: it is
    checked now) and no assessment. The stored row stays as the run wrote it, because the ledger is append-only; only
    this answer changes. Any other decision is as recorded, its constraint named only if it was already the person's
    when the decision was made (_constraint_ref)."""
    active = active_constraints(conn, user_id) if any(row.violated_kinds or _held_now(row) for row in rows) else {}
    gaps = _gaps(conn, user_id, [row.decision_id for row in rows if row.outcome == "withheld"])
    decisions = []
    for row in rows:
        held = _held_now(row)
        kinds = list(row.violated_now if held else row.violated_kinds)
        decisions.append(
            {
                "decision_id": row.decision_id,
                "local_date": row.local_date,
                "outcome": "filtered" if held else row.outcome,
                "reason_codes": ["hard_constraint"] if held else row.reason_codes,
                "constraint": _constraint_ref(kinds, active, None if held else row.created_at),
                "gaps": gaps.get(row.decision_id, []),
                "evaluated": None,
                "job": _job_brief(row),
                "violated_kinds": kinds,
                "unverified": [] if held else row.unverified,
                "rank_position": None if held else row.rank_position,
                "summary": None if held else row.summary,
                "change_note": None if held else row.change_note,
            }
        )
    return decisions


def decisions_page(
    conn: Connection,
    user_id: UUID,
    *,
    outcomes: Sequence[str] | None = None,
    reason: str | None = None,
    since: date | None = None,
    until: date | None = None,
    job_id: int | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """The person's decisions, newest local date first and newest row first, one page by keyset cursor. The outcome and
    reason filters ask about what a decision reads as now (_decisions): a shown match the person's constraints now
    exclude is a filtered one for hard_constraint, and is no longer a shown one."""
    where = ["d.user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    if outcomes:
        where.append(f"({OUTCOME_NOW}) = ANY(CAST(:outcomes AS text[]))")
        params["outcomes"] = list(outcomes)
    if reason:
        where.append(
            f"(CASE WHEN {HELD_NOW} THEN CAST(:reason AS text) = 'hard_constraint'"
            " ELSE CAST(:reason AS text) = ANY(CAST(d.reason_codes AS text[])) END)"
        )
        params["reason"] = reason
    if since:
        where.append("d.local_date >= :since")
        params["since"] = since
    if until:
        where.append("d.local_date <= :until")
        params["until"] = until
    if job_id is not None:
        where.append("d.job_id = :job")
        params["job"] = job_id
    if cursor:
        params["c_day"], params["c_at"], params["c_id"] = decode_cursor(
            cursor, date.fromisoformat, datetime.fromisoformat, UUID
        )
        where.append("(d.local_date, d.created_at, d.id) < (:c_day, :c_at, :c_id)")
    sql = f"""{CURRENT_CONSTRAINTS}
              SELECT {DECISION_COLUMNS} FROM match_decisions d JOIN {JOB_FROM} ON j.id = d.job_id CROSS JOIN c
              WHERE {" AND ".join(where)} ORDER BY d.local_date DESC, d.created_at DESC, d.id DESC LIMIT :n"""
    rows = conn.execute(text(sql), params).all()
    kept = rows[:limit]
    next_cursor = None
    if len(rows) > limit:  # one more row than the page held: there is a next page, after the last row kept
        last = kept[-1]
        next_cursor = encode_cursor(last.local_date.isoformat(), last.created_at.isoformat(), str(last.decision_id))
    return _decisions(conn, user_id, kept), next_cursor


def decision_by_id(conn: Connection, user_id: UUID, decision_id: UUID) -> dict[str, Any] | None:
    """One decision of any outcome with its evidence (none for a job that was never scored). None when it is not the
    person's. It reads as the list does (_decisions): a shown match whose job the person's constraints now exclude is
    filtered, with no evidence. This is the page the web falls back to when match_by_id has no match to open."""
    sql = f"""{CURRENT_CONSTRAINTS}
              SELECT {DECISION_COLUMNS} FROM match_decisions d JOIN {JOB_FROM} ON j.id = d.job_id CROSS JOIN c
              WHERE d.id = :id AND d.user_id = :u"""
    row = conn.execute(text(sql), {"id": decision_id, "u": user_id}).one_or_none()
    if row is None:
        return None
    (decision,) = _decisions(conn, user_id, [row])
    evidence = (
        [] if decision["outcome"] == "filtered" else evidence_of(conn, user_id, [decision_id]).get(decision_id, [])
    )
    return {**decision, "evidence": evidence}


def check_job(conn: Connection, user_id: UUID, job_id: int, day: date) -> dict[str, Any] | None:
    """An unseen job against the person's hard constraints on the spot: the same SQL filter every run uses, no model,
    nothing stored. "filtered" with the constraint it broke, or "not_evaluated" when it passes them all and was never
    scored. None for a job that is not in the index."""
    sql = f"""WITH c AS (SELECT compile_constraints(:u) AS cc)
              SELECT {JOB_COLUMNS}, CAST(constraint_violations(j, c.cc) AS text[]) AS violated
              FROM jobs j JOIN job_sources s ON s.id = j.source_id CROSS JOIN c WHERE j.id = :job AND j.is_listed"""
    row = conn.execute(text(sql), {"u": user_id, "job": job_id}).one_or_none()
    if row is None:
        return None
    violated = list(row.violated)
    return {
        "decision_id": None,
        "local_date": day,
        "outcome": "filtered" if violated else "not_evaluated",
        "reason_codes": ["hard_constraint"] if violated else [],
        "constraint": _constraint_ref(violated, active_constraints(conn, user_id)) if violated else None,
        "gaps": [],
        "evaluated": "on_request",
        "job": _job_brief(row),
    }


def job_decision(conn: Connection, user_id: UUID, job_id: int, day: date) -> dict[str, Any] | None:
    """The newest stored decision for a job, else the on-request check of it (None when the job is not in the index)."""
    stored, _ = decisions_page(conn, user_id, job_id=job_id, limit=1)
    return stored[0] if stored else check_job(conn, user_id, job_id, day)
