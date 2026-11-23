"""Plan 07-03, AI_SYSTEM 7.8: every reason code has a template written by code; a missing parameter raises."""

import pytest

from app.guardian import checks, explain
from app.guardian.explain import TEMPLATES

POLICY_CODES = ["dial_never", "campaign_paused", "campaign_closed", "account_inactive", "type_not_available"]
CHECK_CODES = [
    "citation_unresolved",
    "unsupported_claim",
    "citation_check_missing",
    "consent_missing",
    "constraint_conflict",
    "employer_blocked",
    "restricted_data",
    "budget_exhausted",
    "injection_suspected",
    "limit_reached",
]
PAUSE_REASONS = ["candidate", "budget_exhausted", "scam_suspected"]
PARAMS = {
    "unsupported_claim": {"quote": "I led the migration."},
    "restricted_data": {"detail": "your pay"},
    "scam_suspected": {"quote": "Pay a fee."},
}


@pytest.mark.parametrize("code", POLICY_CODES + CHECK_CODES + PAUSE_REASONS)
def test_every_reason_code_has_a_template(code):
    sentence = explain.explain(code, **PARAMS.get(code, {}))
    assert sentence and sentence[0].isupper() and "{" not in sentence


def test_a_missing_parameter_raises_and_an_unknown_code_raises():
    with pytest.raises(KeyError):
        explain.explain("unsupported_claim")
    with pytest.raises(KeyError):
        explain.explain("no_such_reason")


def test_the_quoted_text_is_the_only_value_that_is_ever_shown():
    shown = explain.explain("unsupported_claim", quote="I led the {migration}.")
    assert shown.endswith('"I led the {migration}."')
    assert explain.explain("scam_suspected", quote="Pay a fee.").endswith('"Pay a fee."')


def test_the_codes_the_checks_can_return_are_all_explained():
    codes = {c for c in TEMPLATES}
    for code in (
        "citation_unresolved",
        "unsupported_claim",
        "citation_check_missing",
        "consent_missing",
        "limit_reached",
    ):
        assert code in codes
    assert checks.CITATION_UNRESOLVED in codes and checks.UNSUPPORTED_CLAIM in codes
