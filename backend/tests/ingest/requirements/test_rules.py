"""The heading-rules baseline (AI_SYSTEM 4.3): no model, rows of the same shape as the validator's.

The postings in this file are invented for the tests. Acme Robotics is not a real company.
"""

import pytest

from app.ingest.requirements.rules import extract
from app.ingest.requirements.validate import MAX_REQUIREMENTS

POSTING = """\
Acme Robotics builds robot arms.
About the role
We are hiring for the arm team.
Responsibilities
- Design robot arms
- Review code
Requirements
- 5+ years of Go
- Bachelor's degree in CS
- Authorized to work in India
- Strong written English
- Kubernetes experience is a plus
Nice to have
- Rust
- Experience with ROS
Benefits
- Free lunch
"""


def test_only_the_lines_under_the_requirement_headings_become_rows():
    rows = extract(POSTING)
    assert [(r.ordinal, r.importance, r.kind, r.text, r.min_years) for r in rows] == [
        (1, "required", "experience", "5+ years of Go", 5),
        (2, "required", "education", "Bachelor's degree in CS", None),
        (3, "required", "authorization", "Authorized to work in India", None),
        (4, "required", "skill", "Strong written English", None),
        (5, "preferred", "skill", "Kubernetes experience is a plus", None),  # "a plus" makes a required line preferred
        (6, "preferred", "skill", "Rust", None),
        (
            7,
            "preferred",
            "skill",
            "Experience with ROS",
            None,
        ),  # starts like a heading but is a line, so no new section
    ]


def test_every_row_is_the_posting_at_its_span_and_the_bullet_mark_is_outside_it():
    rows = extract(POSTING)
    assert all(POSTING[r.start : r.end] == r.text for r in rows)
    assert not any(r.text.startswith(("-", "*", "•")) for r in rows)


@pytest.mark.parametrize("mark", ["-", "*", "•", "–", "• ", "-  "])
def test_any_leading_bullet_mark_is_left_out_of_the_span(mark):
    description = f"Requirements\n{mark}Python"
    (row,) = extract(description)
    assert (row.text, description[row.start : row.end]) == ("Python", "Python")


def test_the_rows_obey_the_same_cap_as_the_validators():
    description = "Requirements\n" + "\n".join(f"Skill number {i}" for i in range(MAX_REQUIREMENTS + 5))
    rows = extract(description)
    assert [r.text for r in rows] == [f"Skill number {i}" for i in range(MAX_REQUIREMENTS)]
    assert [r.ordinal for r in rows] == list(range(1, MAX_REQUIREMENTS + 1))


def test_a_posting_with_no_recognised_heading_returns_nothing():
    assert extract("We build robots.\nPython is great\n5+ years of Go") == []
    assert extract("") == []


def test_headings_ignore_case_a_trailing_colon_and_a_curly_apostrophe():
    description = "WHAT YOU’LL NEED:\nPython\nPREFERRED QUALIFICATIONS:\nRust\nBenefits:\nFree lunch"
    assert [(r.importance, r.text) for r in extract(description)] == [("required", "Python"), ("preferred", "Rust")]


@pytest.mark.parametrize(
    ("heading", "importance"),
    [
        ("Requirements", "required"),
        ("Minimum Qualifications", "required"),
        ("Required skills", "required"),
        ("What you need", "required"),
        ("What you'll need to succeed", "required"),
        ("Who you are", "required"),
        ("Skills", "required"),
        ("Experience", "required"),
        ("You have", "required"),
        ("Must have", "required"),
        ("Preferred qualifications", "preferred"),
        ("Nice to have", "preferred"),
        ("Bonus points", "preferred"),
        ("Desirable", "preferred"),
        ("Plus", "preferred"),
    ],
)
def test_a_requirement_heading_opens_a_section_of_its_importance(heading, importance):
    (row,) = extract(f"{heading}\nPython")
    assert (row.importance, row.text) == (importance, "Python")


@pytest.mark.parametrize(
    "heading",
    [
        "Responsibilities",
        "What you'll do",
        "About us",
        "Benefits & perks",
        "We offer",
        "Compensation",
        "Salary range",
        "How to apply",
        "Equal opportunity employer",
    ],
)
def test_a_stop_heading_ends_a_requirement_section(heading):
    rows = extract(f"Requirements\nPython\n{heading}\nFree lunch")
    assert [r.text for r in rows] == ["Python"]


@pytest.mark.parametrize(
    "line",
    [
        "Requirements analysis experience is essential here",  # more than five words
        "Qualifications internationalization infrastructure instrumentation responsibilities",  # over 60 characters
        "About 3 years of Python",  # a digit
        "Requirements gathering skills.",  # a final period
        "Skills in SQL",  # "skills" is a heading only on a line of its own
        "You have strong SQL skills",  # so is "you have"
    ],
)
def test_a_line_that_only_looks_like_a_heading_is_a_requirement(line):
    (row,) = extract(f"Requirements\n{line}")
    assert row.text == line


def test_a_line_that_says_a_plus_or_preferred_is_preferred_under_requirements():
    description = "Requirements\nPython\nSQL is a plus\nMaster's degree preferred"
    assert [(r.importance, r.kind, r.text) for r in extract(description)] == [
        ("required", "skill", "Python"),
        ("preferred", "skill", "SQL is a plus"),
        ("preferred", "education", "Master's degree preferred"),
    ]


@pytest.mark.parametrize(
    ("line", "kind", "min_years"),
    [
        ("5+ years of Go", "experience", 5),
        ("3-5 years of Java", "experience", 3),  # the lower end is the minimum
        ("At least 10 yrs in sales", "experience", 10),
        ("3-year track record", "experience", 3),
        ("Bachelor's degree in CS", "education", None),
        ("PhD in physics", "education", None),
        ("Authorized to work in India", "authorization", None),
        ("Must have the right to work in the UK", "authorization", None),
        ("Python", "skill", None),
        ("Five years of Go", "skill", None),  # a number written as a word is not read: unknown stays unknown
        ("75 years of history", "skill", None),  # more than the 60 the column allows
    ],
)
def test_kind_and_min_years_come_from_keyword_and_regex_rules(line, kind, min_years):
    (row,) = extract(f"Requirements\n{line}")
    assert (row.kind, row.min_years) == (kind, min_years)
