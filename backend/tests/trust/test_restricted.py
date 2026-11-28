"""The one restricted-data list (AI_SYSTEM 8.3). Pure: no database. The cases are invented, in
tests/twin/fixtures/logic/restricted_cases.json."""

import json
from pathlib import Path

import pytest

from app.trust.api import Restricted, redact_contact, restricted_matches

CASES = json.loads(
    (Path(__file__).parent.parent / "twin" / "fixtures" / "logic" / "restricted_cases.json").read_text("utf-8")
)


def test_fixture_list_is_big_enough() -> None:
    assert len(CASES["positive"]) >= 40
    assert len(CASES["negative"]) >= 20


@pytest.mark.parametrize("case", CASES["positive"], ids=lambda c: c["text"][:40])
def test_positive_cases_are_found(case: dict) -> None:
    kinds = {m.kind for m in restricted_matches(case["text"])}
    assert set(case["kinds"]) <= kinds, kinds


@pytest.mark.parametrize("text", CASES["negative"])
def test_negative_cases_are_clean(text: str) -> None:
    assert restricted_matches(text) == []


def test_topics_follow_kinds() -> None:
    found = restricted_matches("Her salary is high; email ada@example.test")
    assert Restricted("email", "contact_details") in found
    assert Restricted("pay", "current_pay") in found
    assert [m.kind for m in found] == ["email", "pay"]  # fixed order, once each


def test_each_kind_is_reported_once() -> None:
    assert len(restricted_matches("a@example.test b@example.test c@example.test")) == 1


def test_redact_contact_takes_out_contact_but_not_other_words() -> None:
    text = "Built search. Mail ada@example.test or +91 98765 43210, see github.com/ada, 12 Elm Road."
    out = redact_contact(text)
    assert "ada@example.test" not in out and "98765" not in out and "github.com" not in out and "Elm Road" not in out
    assert out.startswith("Built search. Mail ")
    assert restricted_matches(out) == []


def test_redact_contact_leaves_clean_text_alone() -> None:
    assert redact_contact("Led a team of 5 engineers from 2018-2022.") == "Led a team of 5 engineers from 2018-2022."
