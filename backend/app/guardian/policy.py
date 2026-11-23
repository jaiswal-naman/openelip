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

POLICY_VERSION = "policy-2026-11-b"

ACTION_TYPES = ("submit_application", "send_follow_up", "twin_answer")
# The types that always need the candidate's approval, whatever the dial row says (GRD-01; the database agrees).
ALWAYS_APPROVED = frozenset({"submit_application", "send_follow_up"})
LEVELS = ("ask_first", "tell_me_after", "never")


@dataclass(frozen=True)
class TwinPolicy:
    """What key one needs to know about a `twin_answer` besides the dial: whether it is a drafted answer or the
    candidate's relayed reply, and the candidate's permission for its topic (`answer`, `ask_me` or `never`)."""

    kind: str  # answer | relay
    topic_permission: str


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


def decide(action_type: str, *, dial: str, gate: SubjectGate | None, twin: TwinPolicy | None = None) -> PolicyResult:
    """Key one. `dial` is the candidate's level for this type (`ask_first` when no row). A missing subject is closed.

    A `twin_answer` is decided only for a Twin session subject and with its `twin` facts: the standing yes (an answer,
    dial `tell_me_after`, topic permission `answer`, an accepted introduction, which the open subject means) is `allow`;
    a topic set to `never` is `deny topic_not_open`; a drafted answer under dial `never` is denied (the candidate's own
    relayed reply is not, and always waits for the candidate); everything else asks."""
    if action_type not in ACTION_TYPES:
        return PolicyResult("deny", "type_not_available")
    if action_type == "twin_answer":
        if twin is None or gate is None or gate.kind != "twin":
            return PolicyResult("deny", "type_not_available")
        if gate.closed:
            return PolicyResult("deny", "session_closed")
        if not gate.user_active:
            return PolicyResult("deny", "account_inactive")
        if twin.topic_permission == "never":
            return PolicyResult("deny", "topic_not_open")
        if twin.kind == "relay":
            return PolicyResult("ask")
        if dial == "never":
            return PolicyResult("deny", "dial_never")
        if dial == "tell_me_after" and twin.topic_permission == "answer":
            return PolicyResult("allow")
        return PolicyResult("ask")
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
