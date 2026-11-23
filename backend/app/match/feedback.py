"""Save, unsave and dismiss a match, and what a dismissal may propose (plan 04-04; MAT-08).

A click is one append-only feedback_events row. A dismissal reason may also make ONE proposal to change the Career
Record (INDEX decision 10) through app.profile.api.propose_change. A proposal changes nothing: only the person's own
confirmation does (CHT-03), so a click can never edit the record. No model is called; the reason-to-proposal mapping is
the table PROPOSAL_FOR_REASON, and every reason not in it proposes nothing.

Everything runs on the caller's connection, in the request's transaction: the feedback row and its proposal commit
together.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import Connection, func, insert, text

from app.platform import log
from app.platform.errors import ApiError
from app.profile import api as profile

from . import ledger
from .checker import company_key
from .models import feedback_events

PREFERENCE_WEIGHT = -2  # assumption (INDEX decision 10): a dismissal is a nudge against, not a rule
NAME_WORDS = 100  # assumption: characters of a title or company kept in the sentence that says why a change is proposed


@dataclass(frozen=True)
class FeedbackResult:
    """The state of the job after the click, and the proposal it made (the id of the proposed profile event), if any."""

    is_saved: bool
    is_dismissed: bool
    proposal_event_id: UUID | None


class Job(NamedTuple):
    """What a proposal reads of the job: only what the posting states."""

    id: int
    title: str
    company_name: str
    city: str | None
    country: str | None
    seniority: str


class Change(NamedTuple):
    """A record change a dismissal proposes: what propose_change takes, and the sentence that says why."""

    entity: str
    operation: str
    after: dict[str, Any]
    reason: str
    entity_id: UUID | None = None


def _flat(words: str) -> str:
    return " ".join(words.split())


def _short(words: str) -> str:
    flat = _flat(words)
    return flat if len(flat) <= NAME_WORDS else flat[: NAME_WORDS - 1].rstrip() + "…"


def _company(conn: Connection, user_id: UUID, job: Job) -> Change | None:
    """Add the company to the excluded companies. The person has one active exclusion (one constraint per kind), and a
    confirmed proposal replaces it, so an existing one is updated to carry its names and the new one. A company that is
    already excluded under any spelling proposes nothing."""
    existing = profile.compiled_constraints(conn, user_id).get("excluded_company")
    names = list(existing["company_names"]) if existing else []
    if company_key(job.company_name) in {company_key(name) for name in names}:
        return None
    names.append(_flat(job.company_name))
    return Change(
        "constraint",
        "update" if existing else "create",
        {"kind": "excluded_company", "company_names": names, "statement": "Never " + ", ".join(names) + "."},
        f"You dismissed a job at {_short(job.company_name)} as a company you are not interested in.",
        UUID(existing["id"]) if existing else None,
    )


def _preference(kind: str, value: str, statement: str, reason: str) -> Change:
    after = {"kind": kind, "value": value, "weight": PREFERENCE_WEIGHT, "statement": statement}
    return Change("preference", "create", after, reason)


def _role(conn: Connection, user_id: UUID, job: Job) -> Change | None:
    title = _flat(job.title)
    reason = f"You dismissed {_short(title)} at {_short(job.company_name)} as a role you are not interested in."
    return _preference("target_title", title.lower(), f"Fewer roles like {title}.", reason)


def _seniority(conn: Connection, user_id: UUID, job: Job) -> Change | None:
    if job.seniority == "unknown":  # the posting does not say: nothing to propose
        return None
    reason = f"You dismissed {_short(job.title)} at {_short(job.company_name)} because its seniority did not fit."
    return _preference("seniority", job.seniority, f"Fewer {job.seniority}-level roles.", reason)


def _location(conn: Connection, user_id: UUID, job: Job) -> Change | None:
    place = _flat(job.city or job.country or "")  # the most specific place the posting states
    if not place:
        return None
    reason = f"You dismissed {_short(job.title)} at {_short(job.company_name)} because its location did not fit."
    return _preference("location", place.lower(), f"Fewer jobs in {place}.", reason)


# Which dismissal reasons propose something (INDEX decision 10). The rest (skills, pay, already applied, duplicate,
# closed, other, explanation wrong) propose nothing: they say nothing the record could hold.
PROPOSAL_FOR_REASON: dict[str, Callable[[Connection, UUID, Job], Change | None]] = {
    "not_interested_company": _company,
    "not_interested_role": _role,
    "seniority_mismatch": _seniority,
    "location": _location,
}


def _shown_job(conn: Connection, user_id: UUID, decision_id: UUID) -> Job:
    row = conn.execute(
        text(
            """SELECT j.id, j.title, j.company_name, j.city, j.country, j.seniority::text AS seniority
               FROM match_decisions d JOIN jobs j ON j.id = d.job_id
               WHERE d.id = :id AND d.user_id = :u AND d.outcome = 'shown'"""
        ),
        {"id": decision_id, "u": user_id},
    ).one_or_none()
    if row is None:
        raise ledger.not_found()
    return Job(*row)


def _propose(conn: Connection, user_id: UUID, event_id: UUID, job: Job, reason: str) -> UUID | None:
    make = PROPOSAL_FOR_REASON.get(reason)
    change = make(conn, user_id, job) if make else None
    if change is None:
        return None
    try:
        return profile.propose_change(
            conn,
            user_id,
            origin="feedback",
            entity=change.entity,
            operation=change.operation,
            entity_id=change.entity_id,
            after=change.after,
            origin_ref=str(event_id),
            reason=change.reason,
        )
    except ApiError as refused:  # the record's own checks said no (a name it will not keep): the click stands anyway
        log.event("match.proposal_refused", feedback_id=event_id, error_code=refused.code)
        return None


def record_feedback(
    conn: Connection, user_id: UUID, decision_id: UUID, kind: str, reason: str | None, comment: str | None
) -> FeedbackResult:
    """Record a save, an unsave or a dismissal of a shown match, and for a dismissal make the proposal its reason maps
    to. Raises 404 when the decision is not a match shown to this person."""
    job = _shown_job(conn, user_id, decision_id)
    event_id: UUID = conn.execute(
        insert(feedback_events)
        .values(
            user_id=user_id,
            job_id=job.id,
            match_decision_id=decision_id,
            kind=kind,
            dismiss_reason=reason,
            comment=comment,
            created_at=func.clock_timestamp(),  # not now(): two clicks in one transaction keep their order
        )
        .returning(feedback_events.c.id)
    ).scalar_one()
    proposal = _propose(conn, user_id, event_id, job, reason) if kind == "dismiss" and reason else None
    if reason == "explanation_wrong":  # input to the weekly human audit of explanations: ids only
        log.event("match.explanation_reported", decision_id=decision_id, job_id=job.id)
    is_saved, is_dismissed = ledger.feedback_state(conn, user_id, [job.id])[job.id]
    return FeedbackResult(is_saved, is_dismissed, proposal)
