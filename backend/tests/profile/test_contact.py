"""Contact details are masked before resume text could reach a model (threat T-3-07); offsets can be restored."""

import random

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
        # Phone numbers whose groups look like years or dates, and national formats: still phones.
        ("415-555-2021", PHONE),
        ("+44 20 7946 2019", PHONE),
        ("(020) 7946 2024", PHONE),
        ("1-800-555-0199", PHONE),
        ("98765 43210", PHONE),
        ("03-1234-5678", PHONE),
        ("11 4567-8901", PHONE),
        ("06 12 34 56 78", PHONE),
        ("12 34 56 78", PHONE),
        ("12-34-56-78", PHONE),  # 56 is no month, so this is not two MM-YY dates
        ("45 12 2021", PHONE),  # 45 is no day
        ("12 45 2021", PHONE),  # 45 is no month
        ("Tel +91 90000 00001, 2021-03 - 2023-08", f"Tel {PHONE}, 2021-03 - 2023-08"),
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
        "2018-2020 2021-2023",
        "Opened on 2021-03-15",
        "Born 04-03-1998",
        # Employment dates in every common shape: never masked, so the model sees them.
        "2021-03 - 2023-08",
        "Senior Engineer, Example Corp (2021-03 - 2023-08)",
        "Engineer, Globex   2021-03 - 2023-08",
        "03-2021 - 08-2023",
        "03.2021 - 08.2023",
        "03-21 - 08-23",
        "2021 03 - 2023 08",
        "202103 - 202308",
        "03/2021 - 08/2023",
        "03/2021 \N{EN DASH} 08/2023",
        "2021-03 \N{EM DASH} 2023-08",
        "15.03.2021 - 20.08.2023",
        "2021-03-15 - 2023-08-20",
        "Led the team 2021-03 - 2023-08, then 2019 - 2021 before that",
        "Order number 12345678",
        "Reference 12 34 56",  # six digits is too short to be a phone number
        "Python 3.11.5, ASP.NET, Node.js and Vue.js",
        "Led 12 engineers across 3 teams",
    ],
)
def test_years_dates_versions_and_ordinary_numbers_are_not_contact_details(text):
    assert mask(text).text == text


def test_no_generated_date_or_year_span_is_masked():
    rng = random.Random(5)
    shapes = [
        "{y1}-{m1} - {y2}-{m2}",
        "{m1}-{y1} - {m2}-{y2}",
        "{m1}.{y1} - {m2}.{y2}",
        "{m1}-{yy1} - {m2}-{yy2}",
        "{y1} {m1} - {y2} {m2}",
        "{y1}{m1} - {y2}{m2}",
        "{m1}/{y1} - {m2}/{y2}",
        "{y1} - {y2}",
        "{y1}-{y2}",
    ]
    masked = []
    for shape in shapes:
        for _ in range(60):
            y1 = rng.randrange(1995, 2024)
            y2 = rng.randrange(y1, 2031)
            m1, m2 = f"{rng.randrange(1, 13):02d}", f"{rng.randrange(1, 13):02d}"
            span = shape.format(y1=y1, y2=y2, m1=m1, m2=m2, yy1=str(y1)[2:], yy2=str(y2)[2:])
            line = f"Backend Engineer, Example Corp ({span})"
            if mask(line).text != line:
                masked.append(line)

    assert masked == []


def test_generated_phone_numbers_in_common_national_formats_are_all_masked():
    rng = random.Random(6)

    def digits(n):
        return "".join(rng.choice("0123456789") for _ in range(n))

    formats = [
        lambda: f"({digits(3)}) {digits(3)}-{digits(4)}",
        lambda: f"{digits(3)}-{digits(3)}-{digits(4)}",
        lambda: f"{digits(3)}.{digits(3)}.{digits(4)}",
        lambda: f"+1 {digits(3)} {digits(3)} {digits(4)}",
        lambda: f"{digits(5)} {digits(5)}",
        lambda: f"+91 {digits(5)} {digits(5)}",
        lambda: f"+44 {digits(2)} {digits(4)} {digits(4)}",
        lambda: f"0{digits(4)} {digits(6)}",
        lambda: " ".join(digits(2) for _ in range(5)),
        lambda: f"{digits(2)} {digits(4)}-{digits(4)}",
    ]
    leaked = []
    for make in formats:
        for _ in range(60):
            number = make()
            if mask(f"Call {number} today").text != f"Call {PHONE} today":
                leaked.append(number)

    assert leaked == []


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
