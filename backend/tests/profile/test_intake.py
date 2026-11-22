"""REC-03 and REC-04: a dealbreaker that fits a constraint kind is suggested as one; anything else is untyped."""

import pytest

from app.profile.intake import ARRANGEMENTS, EMPLOYMENT_TYPES, ConstraintSuggestion, type_dealbreaker

NOT_CONTRACT = ["full_time", "part_time", "internship", "temporary"]


@pytest.mark.parametrize(
    ("text", "kind", "fields"),
    [
        ("I only want fully remote work", "remote_policy", {"arrangements": ["remote"]}),
        ("fully remote", "remote_policy", {"arrangements": ["remote"]}),
        ("I want to work onsite only", "remote_policy", {"arrangements": ["onsite"]}),
        ("no remote work", "remote_policy", {"arrangements": ["onsite", "hybrid"]}),
        ("I don't want remote work", "remote_policy", {"arrangements": ["onsite", "hybrid"]}),
        ("no onsite", "remote_policy", {"arrangements": ["hybrid", "remote"]}),
        ("not Acme Corp", "excluded_company", {"company_names": ["Acme Corp"]}),
        ("I don't want to work at Acme Corp.", "excluded_company", {"company_names": ["Acme Corp"]}),
        ("not Acme Corp or Globex Inc", "excluded_company", {"company_names": ["Acme Corp", "Globex Inc"]}),
        ("never Bank of Example", "excluded_company", {"company_names": ["Bank of Example"]}),
        ("at least 30 LPA", "salary_floor", {"salary_floor": 3_000_000, "salary_currency": "INR"}),
        ("minimum 120000 USD", "salary_floor", {"salary_floor": 120_000, "salary_currency": "USD"}),
        ("minimum 120k USD", "salary_floor", {"salary_floor": 120_000, "salary_currency": "USD"}),
        ("at least INR 1,20,000", "salary_floor", {"salary_floor": 120_000, "salary_currency": "INR"}),
        ("at least 2.5 crore", "salary_floor", {"salary_floor": 25_000_000, "salary_currency": "INR"}),
        ("minimum ₹30 lakh", "salary_floor", {"salary_floor": 3_000_000, "salary_currency": "INR"}),
        ("no contract work", "employment_type", {"employment_types": NOT_CONTRACT}),
        ("No Contract Work", "employment_type", {"employment_types": NOT_CONTRACT}),
        (
            "I do not want part-time work",
            "employment_type",
            {"employment_types": ["full_time", "contract", "internship", "temporary"]},
        ),
        ("only full-time", "employment_type", {"employment_types": ["full_time"]}),
    ],
)
def test_a_dealbreaker_that_fits_a_constraint_kind_is_suggested_as_one(text, kind, fields):
    assert type_dealbreaker(text) == ConstraintSuggestion(kind, fields)


@pytest.mark.parametrize(
    "text",
    [
        "no heavy travel",
        "No Heavy Travel",
        "good culture",
        "not too much travel",
        "no weekend work",
        "I like remote work",  # a wish, not a rule
        "",
    ],
)
def test_anything_else_is_untyped(text):
    assert type_dealbreaker(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "at least 120000",  # no currency word: never guessed
        "at least $120k",  # a bare dollar sign names no single currency
        "minimum 50000 INR per month",  # not an annual figure
        "at least 30 lakh USD",  # contradicts itself
        "at least 5 years CAD experience",  # a count, not money
    ],
)
def test_a_salary_without_one_clear_annual_currency_amount_is_untyped(text):
    assert type_dealbreaker(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "fully remote and at least 30 LPA",  # two rules fit: half a suggestion would drop the other half
        "no remote or hybrid",
        "no contract or part-time work",
    ],
)
def test_a_statement_that_fits_two_rules_or_two_values_is_untyped(text):
    assert type_dealbreaker(text) is None


def test_suggested_values_come_from_the_enums():
    suggestions = [
        type_dealbreaker("no onsite"),
        type_dealbreaker("no contract work"),
    ]

    assert set(suggestions[0].fields["arrangements"]) <= set(ARRANGEMENTS)
    assert set(suggestions[1].fields["employment_types"]) <= set(EMPLOYMENT_TYPES)
    assert "unknown" not in suggestions[1].fields["employment_types"]  # the column forbids it
