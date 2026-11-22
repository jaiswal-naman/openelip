"""REC-07: a line that states a protected characteristic is detected; work authorization and ordinary words are not."""

import pytest

from app.profile.protected import CATEGORIES, screen

STATES_ONE = [
    ("Date of birth: 04/03/1998", "age"),
    ("DOB: 04/03/1998", "age"),
    ("Age: 27", "age"),
    ("Marital status: single", "marital_status"),
    ("Religion: Jainism", "religion"),
    ("Gender: female", "gender"),
    ("Sex - F", "gender"),
    ("Sexual orientation: prefer not to say", "sexual_orientation"),
    ("Nationality: Indian", "nationality"),
    ("Race: example", "ethnicity"),
    ("Caste category: OBC", "caste"),
    ("I have a disability", "disability"),
    ("I am pregnant", "pregnancy"),
]


@pytest.mark.parametrize(("text", "category"), STATES_ONE)
def test_a_protected_characteristic_returns_its_category(text, category):
    assert screen(text) == category


def test_every_category_has_a_covering_example():
    assert {category for _, category in STATES_ONE} == set(CATEGORIES)


@pytest.mark.parametrize(
    "text",
    [
        "Authorized to work in India",
        "Work authorization: US citizen",
        "Chief Age Officer tooling",
        "age verification service",
        "Race condition debugging in the queue worker",
        "Gender pay gap dashboard for HR analytics",
        "Nationality is not used as a filter in this system",
        "Python, Go and SQL",
        "",
    ],
)
def test_work_authorization_and_ordinary_words_are_not_flagged(text):
    assert screen(text) is None


def test_the_result_is_a_category_name_never_the_matched_text():
    result = screen("Date of birth: 04/03/1998")

    assert result in CATEGORIES
    assert "1998" not in result
