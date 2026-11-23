"""Who is asked about a role, and the ask itself (HIR-04, HIR-06, HIR-07): the candidate pool, and one private
introduction per candidate a role run selected.

An ask is the only row an employer-side process creates about a candidate, and it creates it as the worker (app_api
has no INSERT grant on introductions). The database is the last guard: `introductions_guard` refuses an ask for a role
that is not open, without the candidate's open_to_introductions consent, that breaks one of their hard constraints (a
blocked employer is one), or from an organization with an overdue answer; `introductions_once` makes a second ask for
the same role and candidate a no-op. Every ask runs in a savepoint of its own, so one refusal skips one candidate.
"""

import json
import logging
from dataclasses import dataclass
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.match import api as match
from app.platform import log
from app.trust import api as trust

from . import preview

ASK_FRESHNESS = timedelta(hours=24)  # assumption (06-INDEX 23): asks come only from a role run this recent
CHECK_VIOLATION = "23514"


@dataclass(frozen=True)
class OpenRole:
    id: UUID
    organization_id: UUID
    job_id: int
    organization_is_synthetic: bool


@dataclass(frozen=True)
class AskOutcome:
    """What one pass of asking did: introductions created, candidates skipped (stale evidence, consent withdrawn, a
    constraint added since the run, or already asked), and whether it stopped because the organization owes an
    answer."""

    asked: int
    skipped: int
    paused: bool


def open_role(conn: Connection, role_id: UUID) -> OpenRole | None:
    """The open, published role (it has its unlisted job), and whether its organization is synthetic."""
    row = conn.execute(
        text(
            """SELECT r.id, r.organization_id, r.job_id, o.is_synthetic
                 FROM roles r JOIN organizations o ON o.id = r.organization_id
                WHERE r.id = :role AND r.status = 'open' AND r.job_id IS NOT NULL"""
        ),
        {"role": role_id},
    ).one_or_none()
    return None if row is None else OpenRole(row.id, row.organization_id, row.job_id, row.is_synthetic)


def is_paused(conn: Connection, organization_id: UUID) -> bool:
    """Whether the organization has an answer overdue, so it may ask nobody new (HIR-06)."""
    return bool(
        conn.execute(text("SELECT organization_has_overdue_answers(:org)"), {"org": organization_id}).scalar_one()
    )


# The pool's predicates, in one place: pool() applies them to every account, and the re-check at ask time to the people
# a role run selected, so the two cannot drift. Hard constraints are not here: the role run applies them first.
POOL_WHERE = """u.status = 'active' AND u.record_confirmed_at IS NOT NULL
                  AND has_active_consent(u.id, 'open_to_introductions')
                  AND (:synthetic OR NOT u.is_synthetic)
                  AND NOT EXISTS (SELECT 1 FROM introductions i WHERE i.role_id = :role AND i.user_id = u.id)
                  AND NOT EXISTS (SELECT 1 FROM org_members m WHERE m.organization_id = :org AND m.user_id = u.id
                                                                AND m.status = 'active')"""


def _qualified(conn: Connection, role: OpenRole, only: Sequence[UUID] | None = None) -> list[UUID]:
    """The accounts that satisfy every pool predicate for this role now (all of them, or only those named), by id."""
    rows = conn.execute(
        text(
            f"""SELECT u.id FROM users u
                 WHERE {POOL_WHERE} AND (CAST(:only AS uuid[]) IS NULL OR u.id = ANY(CAST(:only AS uuid[])))
                 ORDER BY u.id"""
        ),
        {
            "role": role.id,
            "org": role.organization_id,
            "synthetic": role.organization_is_synthetic,
            "only": None if only is None else list(only),
        },
    ).scalars()
    found = []
    for user_id in rows:
        try:
            trust.require_consent(conn, user_id, "ai_processing")
        except trust.ConsentRequired:
            continue  # their wording is out of date or withdrawn: nobody is scored on it
        found.append(user_id)
    return found


def pool(conn: Connection, role: OpenRole) -> list[UUID]:
    """The candidates who may be asked about this role: active accounts with a confirmed record, open to introductions
    and agreed to AI processing, not asked about this role yet, not members of the organization, and, for a real
    organization, not synthetic (06-INDEX 24). Hard constraints are not read here: the role run applies them first."""
    return _qualified(conn, role)


def _refusal(error: DBAPIError) -> str | None:
    """The prefix of the database's own refusal (consent_missing, hard_constraint, answer_clock), else None."""
    origin = error.orig
    if getattr(origin, "sqlstate", None) != CHECK_VIOLATION:
        return None
    message = getattr(getattr(origin, "diag", None), "message_primary", None) or ""
    for prefix in ("consent_missing", "hard_constraint", "answer_clock"):
        if message.startswith(prefix):
            return prefix
    return None


def _fresh(conn: Connection, run_id: UUID) -> bool:
    return bool(
        conn.execute(
            text(
                """SELECT EXISTS (SELECT 1 FROM match_runs WHERE id = :run AND status = 'completed'
                                     AND finished_at > now() - make_interval(secs => :seconds))"""
            ),
            {"run": run_id, "seconds": ASK_FRESHNESS.total_seconds()},
        ).scalar_one()
    )


def _already_asked(conn: Connection, role: OpenRole, user_id: UUID) -> bool:
    """A shortcut, so a sweep does not build a preview for a candidate asked long ago. It is not the guard: the unique
    key introductions_once and ON CONFLICT DO NOTHING are, for two jobs that race."""
    return bool(
        conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM introductions WHERE role_id = :role AND user_id = :u)"),
            {"role": role.id, "u": user_id},
        ).scalar_one()
    )


def ask_from_run(conn: Connection, role: OpenRole, run_id: UUID) -> AskOutcome:
    """Ask each candidate the run introduced who has no introduction for this role yet. Only a run finished in the last
    24 hours is used. Stops at the first answer_clock refusal (the organization owes an answer): the rest wait for the
    sweep to pick them up after it is answered."""
    asked = skipped = no_longer = 0
    if not _fresh(conn, run_id):
        return AskOutcome(0, 0, False)
    selections = match.role_selections(conn, run_id)
    # The run is up to a day old: re-check the pool's predicates for the selected people now, in one query, and skip
    # whoever withdrew ai_processing, joined the organization or no longer fits the synthetic rule since.
    still = set(_qualified(conn, role, [selection.user_id for selection in selections]))
    for selection in selections:
        if _already_asked(conn, role, selection.user_id):
            continue
        if selection.user_id not in still:
            no_longer += 1
            skipped += 1
            continue
        frozen = preview.build(conn, selection.user_id, selection)
        if frozen is None:
            skipped += 1
            continue
        try:
            with conn.begin_nested():
                created = conn.execute(
                    text(
                        """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id,
                                                      share_preview)
                           VALUES (:org, :role, :u, :decision, CAST(:preview AS jsonb))
                           ON CONFLICT ON CONSTRAINT introductions_once DO NOTHING RETURNING id"""
                    ),
                    {
                        "org": role.organization_id,
                        "role": role.id,
                        "u": selection.user_id,
                        "decision": selection.decision_id,
                        "preview": json.dumps(frozen, sort_keys=True, ensure_ascii=False),
                    },
                ).first()
        except DBAPIError as error:
            refusal = _refusal(error)
            if refusal is None:
                raise
            log.event("hiring.ask_refused", level=logging.INFO, role_id=role.id, error_code=refusal)
            if refusal == "answer_clock":
                return AskOutcome(asked, skipped, True)
            skipped += 1
            continue
        if created is None:
            continue  # asked meanwhile: introductions_once says no
        asked += 1
    log.event(
        "hiring.asked", role_id=role.id, run_id=run_id, asked=asked, skipped=skipped, no_longer_eligible=no_longer
    )
    return AskOutcome(asked, skipped, False)
