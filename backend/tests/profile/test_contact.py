"""Contact details are masked before resume text could reach a model (threat T-3-07); offsets can be restored."""

import pytest

from app.profile.resume.contact import ADDRESS, EMAIL, PHONE, URL, mask


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Reach me: jane@example.com or +91 90000 00001.", f"Reach me: {EMAIL} or {PHONE}."),
        ("jane.doe+jobs@mail.example.co.in", EMAIL),
        ("Phone: 415 555 0132", f"Phone: {PHONE}"),
        ("(415) 555-0132", PHONE),
        ("415-555-0132", PHONE),
        ("+1 415 555 0132", PHONE),
        ("+44 20 7946 0958", PHONE),
        ("9000000001", PHONE),
        ("555-0132", PHONE),  # seven digits is the shortest number taken for a phone
        ("https://example.com/in/jane-doe", URL),
        ("See www.example.com.", f"See {URL}."),
        ("github.com/janedoe", URL),
        ("GitHub.com/janedoe", URL),
        ("Portfolio: janedoe.dev/projects", f"Portfolio: {URL}"),
        ("12 Example Street, Springfield", f"{ADDRESS}, Springfield"),
        ("221B Baker Street", ADDRESS),
    ],
)
def test_contact_details_become_placeholders(text, masked):
    assert mask(text).text == masked


@pytest.mark.parametrize(
    "text",
    [
        "Senior Engineer, Example Corp 2021-2024",
        "Example University, 2019 - 2021",
        "Jan 2021 - Mar 2024",
        "2018 2020 2022",
        "Opened on 2021-03-15",
        "Born 04-03-1998",
        "Order number 12345678",
        "Reference 12 34 56",  # six digits is too short to be a phone number
        "Python 3.11.5, ASP.NET, Node.js and Vue.js",
        "Led 12 engineers across 3 teams",
    ],
)
def test_years_dates_versions_and_ordinary_numbers_are_not_contact_details(text):
    assert mask(text).text == text


def test_a_line_with_several_kinds_loses_every_original_detail():
    text = "jane@example.com | +91 90000 00001 | www.example.com/jane | 12 Example Street"

    masked = mask(text)

    assert masked.text == f"{EMAIL} | {PHONE} | {URL} | {ADDRESS}"
    assert [text[r.start : r.end] for r in masked.replacements] == [
        "jane@example.com",
        "+91 90000 00001",
        "www.example.com/jane",
        "12 Example Street",
    ]


def test_a_domain_inside_an_email_is_masked_once_as_the_email():
    assert mask("write to jane@example.com").text == f"write to {EMAIL}"


def test_offsets_in_the_masked_text_map_back_to_the_original():
    text = "Mail jane@example.com, call +91 90000 00001, see github.com/jane. Done."
    masked = mask(text)

    for word in ("call", "see", "Done"):
        assert text[masked.original_offset(masked.text.index(word)) :].startswith(word)
    # The start of a placeholder maps to the start of what it replaced.
    assert masked.original_offset(masked.text.index(EMAIL)) == text.index("jane@")
    assert masked.original_offset(masked.text.index(PHONE)) == text.index("+91")
    # The end of the text maps to the end of the original, however much the masking shortened it.
    assert masked.original_offset(len(masked.text)) == len(text)


def test_text_without_contact_details_is_unchanged_and_empty_text_is_fine():
    assert mask("Built a billing service in Go.").replacements == ()
    assert mask("").text == ""
