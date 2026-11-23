"""What other modules may call from app.guardian (the shared contracts in the Phase 7 index). Nothing else here is a
public interface.

  SubjectGate, register_subject, register_citation_check   the registries a higher module fills when it is imported
  open_run, close_run, record, timeline, entry, last_state_change   the flight recorder
  propose_action                a proposed outside action: key one, key two, and the approval or the block
  approval_view, list_approvals, pending_counts, decide, undo       what the candidate sees and does (API login)
  release                       the worker's half of an approval (the `guardian.release` job)
  mark_executed, cancel_open, hold, unhold                          the campaign's side of an action's life
  spent_microusd, budget_ok     model spend against the budgets
  scam_signals, explain         rule-based signals and code-written reasons
  get_autonomy, put_autonomy    the dial
  payload_hash                  what the candidate approves
"""

from . import autonomy
from .approvals import (
    APPROVAL_TTL,
    EDITABLE_FIELDS,
    Decision,
    Proposed,
    UndoResult,
    approval_view,
    cancel_open,
    decide,
    hold,
    list_approvals,
    mark_executed,
    payload_hash,
    pending_counts,
    propose_action,
    release,
    undo,
    unhold,
)
from .autonomy import AutonomyView
from .budget import budget_ok, spent_microusd
from .checks import GUARDIAN_VERSION, CheckResult, register_citation_check
from .explain import explain
from .policy import POLICY_VERSION, SubjectGate, register_subject
from .recorder import close_run, entry, last_state_change, open_run, record, timeline
from .scam import ScamSignal
from .scam import signals as scam_signals

get_autonomy = autonomy.view
put_autonomy = autonomy.put

__all__ = [
    "APPROVAL_TTL",
    "EDITABLE_FIELDS",
    "GUARDIAN_VERSION",
    "POLICY_VERSION",
    "AutonomyView",
    "CheckResult",
    "Decision",
    "Proposed",
    "ScamSignal",
    "SubjectGate",
    "UndoResult",
    "approval_view",
    "budget_ok",
    "cancel_open",
    "close_run",
    "decide",
    "entry",
    "explain",
    "get_autonomy",
    "hold",
    "last_state_change",
    "list_approvals",
    "mark_executed",
    "open_run",
    "payload_hash",
    "pending_counts",
    "propose_action",
    "put_autonomy",
    "record",
    "register_citation_check",
    "register_subject",
    "release",
    "scam_signals",
    "spent_microusd",
    "timeline",
    "undo",
    "unhold",
]
