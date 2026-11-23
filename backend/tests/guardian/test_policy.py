"""Plan 07-03, key one: the policy engine decides from the type, the dial and the subject's state, and never allows
the two always-approved types."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.guardian import policy
from app.guardian.policy import SubjectGate, decide

GATE = SubjectGate(
    kind="campaign",
    id=uuid4(),
    user_id=uuid4(),
    user_active=True,
    paused_reason=None,
    closed=False,
    job_id=1,
    organization_id=None,
    follow_up_email="talent@lumen.example",
    budget_microusd=750_000,
)


def verdict(action_type, dial="ask_first", gate=GATE):
    result = decide(action_type, dial=dial, gate=gate)
    return result.decision, result.reason_code


def test_the_policy_version_is_named():
    assert policy.POLICY_VERSION == "policy-2026-11-b"


@pytest.mark.parametrize("action_type", ["submit_application", "send_follow_up"])
def test_the_two_always_approved_types_ask_at_ask_first(action_type):
    assert verdict(action_type) == ("ask", None)


@pytest.mark.parametrize("dial", ["ask_first", "tell_me_after", "never", "anything"])
@pytest.mark.parametrize("action_type", ["submit_application", "send_follow_up"])
def test_the_two_always_approved_types_are_never_allowed_whatever_the_dial_row_says(action_type, dial):
    assert verdict(action_type, dial)[0] in ("ask", "deny")


def test_a_dial_set_to_never_denies_with_dial_never():
    assert verdict("send_follow_up", "never") == ("deny", "dial_never")
    assert verdict("submit_application", "never") == ("deny", "dial_never")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"paused_reason": "candidate"}, "campaign_paused"),
        ({"paused_reason": "budget_exhausted"}, "campaign_paused"),
        ({"closed": True}, "campaign_closed"),
        ({"user_active": False}, "account_inactive"),
    ],
)
def test_the_subjects_state_denies_before_the_dial_is_read(changes, code):
    assert verdict("send_follow_up", "ask_first", replace(GATE, **changes)) == ("deny", code)


def test_a_missing_subject_is_treated_as_closed():
    assert verdict("send_follow_up", gate=None) == ("deny", "campaign_closed")


@pytest.mark.parametrize("action_type", ["twin_answer", "buy_a_boat", ""])
@pytest.mark.parametrize("dial", ["ask_first", "tell_me_after", "never"])
def test_the_twin_and_every_unknown_type_are_not_available(action_type, dial):
    assert verdict(action_type, dial) == ("deny", "type_not_available")


def test_the_subject_registry_reads_through_the_registered_reader():
    seen = []
    policy.register_subject("probe", lambda conn, subject_id: seen.append(subject_id) or GATE)
    try:
        assert policy.read_gate(None, "probe", GATE.id) is GATE and seen == [GATE.id]
        assert policy.read_gate(None, "unknown_kind", GATE.id) is None
    finally:
        policy._readers.pop("probe")
