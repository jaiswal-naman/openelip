"""What other modules may call from app.guardian (the shared contracts in the Phase 7 index). Nothing else here is a
public interface.

  SubjectGate, register_subject, register_citation_check   the registries a higher module fills when it is imported
  open_run, close_run, record, timeline, entry, last_state_change, list_runs   the flight recorder
  propose_action                a proposed outside action: key one, key two, and the approval or the block
  approval_view, list_approvals, pending_counts, decide, undo       what the candidate sees and does (API login)
  release                       the worker's half of an approval (the `guardian.release` job)
  mark_executed, cancel_open, hold, unhold                          the campaign's side of an action's life
  spent_microusd, budget_ok     model spend against the budgets
  session_spent_microusd        a Twin session's model spend, and SESSION_COST_MICROUSD, its cap
  scam_signals, explain         rule-based signals and code-written reasons
  get_autonomy, put_autonomy    the dial
  payload_hash                  what the candidate approves
  register_approve_hook         a higher module (the Twin) checks and completes an approval of one action type
  HookResult, approve_hook      what such a hook returns, and the route's lookup of it
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
from .budget import budget_ok, session_spent_microusd, spent_microusd
from .checks import GUARDIAN_VERSION, SESSION_COST_MICROUSD, CheckResult, register_citation_check
from .explain import explain
from .policy import POLICY_VERSION, SubjectGate, register_subject
from .recorder import RunRow, close_run, entry, last_state_change, list_runs, open_run, record, timeline
from .scam import ScamSignal
from .scam import signals as scam_signals

get_autonomy = autonomy.view
put_autonomy = autonomy.put


@dataclass(frozen=True)
class HookResult:
    """What an approve hook hands back: `edits` the approval is decided with (None: none) and `extra` fields the
    approve response gains."""

    edits: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# hook(conn, user_id, view, *, reply, save_to_record) -> HookResult. `view` is `approval_view`'s dict. The hook runs on
# the caller's API connection, before `decide`, and refuses by raising ApiError; the route calls it only for an approval
# of its action type (and for none, a reply is `422` as in Phase 7).
ApproveHook = Callable[..., HookResult]
_approve_hooks: dict[str, ApproveHook] = {}


def register_approve_hook(action_type: str, hook: ApproveHook) -> None:
    """Teach the approve route about one action type. Registering a type again replaces its hook."""
    _approve_hooks[action_type] = hook


def approve_hook(action_type: str) -> ApproveHook | None:
    return _approve_hooks.get(action_type)


__all__ = [
    "APPROVAL_TTL",
    "ApproveHook",
    "HookResult",
    "EDITABLE_FIELDS",
    "GUARDIAN_VERSION",
    "POLICY_VERSION",
    "SESSION_COST_MICROUSD",
    "AutonomyView",
    "CheckResult",
    "Decision",
    "Proposed",
    "ScamSignal",
    "SubjectGate",
    "RunRow",
    "UndoResult",
    "approval_view",
    "approve_hook",
    "budget_ok",
    "cancel_open",
    "close_run",
    "decide",
    "entry",
    "explain",
    "get_autonomy",
    "hold",
    "last_state_change",
    "list_runs",
    "list_approvals",
    "mark_executed",
    "open_run",
    "payload_hash",
    "pending_counts",
    "propose_action",
    "put_autonomy",
    "record",
    "register_approve_hook",
    "register_citation_check",
    "register_subject",
    "release",
    "scam_signals",
    "session_spent_microusd",
    "spent_microusd",
    "timeline",
    "undo",
    "unhold",
]
