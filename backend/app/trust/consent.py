"""Consent records (TRU-05; ARCHITECTURE 8.1; DATA_MODEL 3.3): grant, withdraw, check and list.

`consents` is append-only. A grant is active while no withdrawal row points at it; withdrawing inserts those rows and
never changes a grant. The AI processing wording names the configured model providers, so its version is a hash of
their names (and of DISCLOSURE_VERSION): when the operator changes the providers, a grant made under the old list no
longer matches, and require_consent says so until the person agrees to the new wording. Search, the record and the
ledger never call require_consent, so they keep working with no consent at all.
"""

import hashlib
import logging
from collections.abc import Sequence
from typing import Any, Literal, get_args
from uuid import UUID

from sqlalchemy import Connection, Row, func, insert, text

from app.platform import config, log
from app.platform.auth import ConsentChange, ConsentPurpose, ConsentState
from app.platform.errors import ApiError
from app.platform.llm import registry

from . import audit, models

# Placeholder, not legal text: the owner approves the real wording (plan 03-01, manual check 2).
DISCLOSURE_VERSION = "2026-11-a"
# The purposes this module records: the ones a person gives in onboarding and settings. introduction and agent_access
# are recorded by the action that needs them (API.md 2.3), with the organization or token they are scoped to.
PURPOSES = get_args(ConsentPurpose)

Reason = Literal["missing", "providers_changed"]

# A grant is active while no withdrawal row points at it (DATA_MODEL 3.3).
_ACTIVE_GRANTS = """SELECT g.id, g.purpose, g.text_version, g.created_at FROM consents g
                    WHERE g.user_id = :user AND g.action = 'granted'
                      AND NOT EXISTS (SELECT 1 FROM consents w WHERE w.withdraws_id = g.id)"""
_NEWEST_FIRST = " ORDER BY g.created_at DESC, g.id DESC"

# One statement, so two withdrawals at once cannot both end the same grant: the unique index turns the second into
# DO NOTHING. clock_timestamp() (not the transaction's now()) keeps rows made in one transaction in order.
_WITHDRAW = """INSERT INTO consents (user_id, purpose, action, withdraws_id, text_version, created_at)
               SELECT g.user_id, g.purpose, 'withdrawn', g.id, g.text_version, clock_timestamp()
               FROM consents g
               WHERE g.user_id = :user AND g.purpose = :purpose AND g.action = 'granted'
                 AND NOT EXISTS (SELECT 1 FROM consents w WHERE w.withdraws_id = g.id)
               ON CONFLICT (withdraws_id) WHERE withdraws_id IS NOT NULL DO NOTHING
               RETURNING id"""


class ConsentRequired(ApiError):
    """A consent is not on file, or the AI wording it was given under has changed.

    The API answers 403 consent_required (API.md 1.11) with details.purpose and details.reason. It is an ApiError so the
    existing handler maps it with no wiring; a job catches it by name.
    """

    def __init__(self, reason: Reason, purpose: str = "ai_processing") -> None:
        super().__init__(403, "consent_required", _message(reason, purpose), {"purpose": purpose, "reason": reason})
        self.reason = reason
        self.purpose = purpose


def _message(reason: Reason, purpose: str) -> str:
    if purpose != "ai_processing":
        return "This needs a consent you have not given."
    if reason == "providers_changed":
        return "The providers that process your data have changed. Review them and agree to AI processing again."
    return "AI features are off because you have not agreed to AI processing."


def _check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(
            f"{purpose!r} is not recorded here: introduction and agent_access are recorded by their own action"
        )


def provider_names() -> list[str]:
    """The model providers a task would be sent to right now, by the names /v1/privacy/subprocessors shows, sorted."""
    routes = registry.active_routes(config.get())
    return sorted({route.provider: route.label for route in routes}.values())


def _version(names: list[str]) -> str:
    return hashlib.sha256("\n".join([DISCLOSURE_VERSION, *names]).encode()).hexdigest()[:12]


def ai_processing_text_version() -> str:
    """The version of the AI processing wording in force now: 12 characters, changing with the provider list."""
    return _version(provider_names())


def _active_grants(conn: Connection, user_id: UUID, purpose: str) -> list[Row[Any]]:
    """The grants of this purpose that no withdrawal ends, newest first. Normally none or one."""
    sql = _ACTIVE_GRANTS + " AND g.purpose = :purpose" + _NEWEST_FIRST
    return list(conn.execute(text(sql), {"user": user_id, "purpose": purpose}))


def grant_consent(conn: Connection, user_id: UUID, purpose: str, text_version: str) -> UUID:
    """Record that the person agreed to `purpose` under this wording, and audit it. Returns the grant's id.

    Agreeing again to the wording already on file records nothing new. The wording is stored as given: the caller must
    have shown it (apply_changes refuses a stale AI wording before it gets here).
    """
    _check_purpose(purpose)
    if not text_version:
        raise ValueError("a consent names the wording version the person saw")
    for grant in _active_grants(conn, user_id, purpose):
        if grant.text_version == text_version:
            grant_id: UUID = grant.id
            return grant_id
    grant_id = conn.execute(
        insert(models.consents)
        .values(
            user_id=user_id,
            purpose=purpose,
            action="granted",
            text_version=text_version,
            created_at=func.clock_timestamp(),
        )
        .returning(models.consents.c.id)
    ).scalar_one()
    audit.audit_log(
        conn,
        "consent.granted",
        "candidate",
        user_id,
        {"purpose": purpose, "text_version": text_version},
        entity_table="consents",
        entity_id=str(grant_id),
    )
    return grant_id


def withdraw_consent(conn: Connection, user_id: UUID, purpose: str) -> int:
    """End every active grant of `purpose` and audit each. Returns how many were ended; 0 when none was active."""
    _check_purpose(purpose)
    ended = conn.execute(text(_WITHDRAW), {"user": user_id, "purpose": purpose}).scalars().all()
    for withdrawal_id in ended:
        audit.audit_log(
            conn,
            "consent.withdrawn",
            "candidate",
            user_id,
            {"purpose": purpose},
            entity_table="consents",
            entity_id=str(withdrawal_id),
        )
    return len(ended)


def require_consent(conn: Connection, user_id: UUID, purpose: str) -> None:
    """Raise ConsentRequired unless the person has an active grant of `purpose`.

    For ai_processing the grant must also carry the wording version in force now, else the reason is providers_changed.
    Fails closed: any error while reading the consents or the provider list counts as missing. The read runs in a
    savepoint, so a database error leaves the caller's transaction usable (a job can still record that it failed).
    """
    _check_purpose(purpose)
    try:
        with conn.begin_nested():
            versions = [grant.text_version for grant in _active_grants(conn, user_id, purpose)]
            current = ai_processing_text_version() if purpose == "ai_processing" else None
    except Exception as error:  # fail closed: whatever went wrong, the consent cannot be shown
        log.event("consent.check_failed", level=logging.ERROR, kind=purpose, error=error)
        raise ConsentRequired("missing", purpose) from None
    if not versions:
        raise ConsentRequired("missing", purpose)
    if current is not None and current not in versions:
        raise ConsentRequired("providers_changed", purpose)


def current_consents(conn: Connection, user_id: UUID) -> list[ConsentState]:
    """One entry per consent a person gives in onboarding and settings, in a fixed order, granted or not.

    The ai_processing entry also carries the provider names and the wording version a new grant must carry, so a person
    who has not agreed yet can be shown what they are agreeing to.
    """
    newest: dict[str, Row[Any]] = {}
    for row in conn.execute(text(_ACTIVE_GRANTS + _NEWEST_FIRST), {"user": user_id}):
        newest.setdefault(row.purpose, row)
    names = provider_names()
    states = []
    for purpose in PURPOSES:
        grant = newest.get(purpose)
        is_ai = purpose == "ai_processing"
        states.append(
            ConsentState(
                purpose=purpose,
                granted=grant is not None,
                granted_at=grant.created_at if grant else None,
                text_version=grant.text_version if grant else None,
                providers=names if is_ai else None,
                current_text_version=_version(names) if is_ai else None,
            )
        )
    return states


def apply_changes(conn: Connection, user_id: UUID, changes: Sequence[ConsentChange]) -> None:
    """PATCH /v1/me: record each consent the person granted or withdrew, in order.

    A grant of the AI wording must carry the version in force now. Otherwise nothing is recorded and the answer is
    409 conflict with details.state consent_text_changed, so nobody agrees to wording they were not shown (T-3-01). The
    API.md catalogue has no code of its own for this. The caller's transaction rolls back, so the whole change is
    all-or-nothing.
    """
    for change in changes:
        if not change.granted:
            withdraw_consent(conn, user_id, change.purpose)
            continue
        version = change.text_version
        if version is None:  # the request model refuses this; the check keeps the type honest
            raise ValueError("a grant names its wording")
        if change.purpose == "ai_processing" and version != (current := ai_processing_text_version()):
            raise ApiError(
                409,
                "conflict",
                "The AI processing wording has changed. Review it and agree again.",
                {"state": "consent_text_changed", "current_version": current},
            )
        grant_consent(conn, user_id, change.purpose, version)
