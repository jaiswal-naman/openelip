"""Key one: the policy engine (AI_SYSTEM 7.3 and 7.5).

decide() turns an action type, the candidate's dial and the state of the thing the action belongs to into allow, ask or
deny. It encodes the rules that can never be relaxed (N1, N2, N8, N9 and the always-approved types). The database
trigger
reads the dial again when an action is cleared to run, so a bug here cannot skip it.

The subject registry lives here too: the Guardian (layer 4) may not import campaigns (layer 6), so a higher module
registers a reader that turns its row into a SubjectGate when it is imported (INDEX decision 6).
"""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection

POLICY_VERSION = "policy-2026-11-a"

ACTION_TYPES = ("submit_application", "send_follow_up", "twin_answer")
# The types that always need the candidate's approval, whatever the dial row says (GRD-01; the database agrees).
ALWAYS_APPROVED = frozenset({"submit_application", "send_follow_up"})
# Types whose checks do not exist yet: the Twin's arrive in Phase 9.
NOT_AVAILABLE = frozenset({"twin_answer"})
LEVELS = ("ask_first", "tell_me_after", "never")


@dataclass(frozen=True)
class SubjectGate:
    """What the Guardian needs to know about the thing an action belongs to (a campaign), read through the registry."""

    kind: str
    id: UUID
    user_id: UUID
    user_active: bool
    paused_reason: str | None
    closed: bool
    job_id: int
    organization_id: UUID | None
    follow_up_email: str | None
    budget_microusd: int


SubjectReader = Callable[[Connection, UUID], SubjectGate | None]
_readers: dict[str, SubjectReader] = {}


def register_subject(kind: str, reader: SubjectReader) -> None:
    """Teach the Guardian to read one kind of subject. Registering a kind again replaces its reader."""
    _readers[kind] = reader


def read_gate(conn: Connection, kind: str, subject_id: UUID) -> SubjectGate | None:
    """The subject's gate, or None when it is gone or this connection cannot see it (or no reader is registered)."""
    reader = _readers.get(kind)
    return None if reader is None else reader(conn, subject_id)


@dataclass(frozen=True)
class PolicyResult:
    decision: str  # allow | ask | deny
    reason_code: str | None = None


def decide(action_type: str, *, dial: str, gate: SubjectGate | None) -> PolicyResult:
    """Key one. `dial` is the candidate's level for this type (`ask_first` when no row). A missing subject is closed."""
    if action_type not in ACTION_TYPES or action_type in NOT_AVAILABLE:
        return PolicyResult("deny", "type_not_available")
    if gate is None or gate.closed:
        return PolicyResult("deny", "campaign_closed")
    if not gate.user_active:
        return PolicyResult("deny", "account_inactive")
    if gate.paused_reason is not None:
        return PolicyResult("deny", "campaign_paused")
    if dial == "never":
        return PolicyResult("deny", "dial_never")
    if action_type in ALWAYS_APPROVED or dial != "tell_me_after":
        return PolicyResult("ask")
    return PolicyResult("allow")
