"""Contact masking (AI_SYSTEM 2.6 and 4.2, threat T-3-07).

Email addresses, phone numbers, links and street addresses become placeholders before resume text is put into any
prompt, so a provider never receives them. Pure text in, text out: nothing here logs, stores or raises on odd input.

    masked = mask("Reach me: jane@example.com or +91 90000 00001.")
    masked.text                    # "Reach me: [EMAIL] or [PHONE]."
    masked.original_offset(i)      # where offset i of the masked text sits in the original text

The placeholders are upper case here, as plan 03-02 and 03-03 specify; AI_SYSTEM 2.6 writes them in lower case.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

EMAIL = "[EMAIL]"
PHONE = "[PHONE]"
URL = "[URL]"
ADDRESS = "[ADDRESS]"

_EMAIL_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# A scheme or www. prefix, or a host on a common top-level domain written in lower case (github.com/jane, jane.dev).
# An upper-case domain is left alone on purpose: ASP.NET is a skill, not a link.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|\b[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.(?:com|org|net|dev|me|io|co|in|ai|app|xyz|site|tech)\b"
    r"(?:/[^\s<>\"']*)?"
)
# Digits with spaces, dots, dashes or brackets between them, an optional leading +. _is_phone() then decides.
_PHONE_RE = re.compile(r"(?<!\w)(?:\+ ?)?\(?\d[\d \u00a0().-]{5,}\d(?!\w)")
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}[A-Za-z]?,?\s+(?:[A-Z][\w'.-]*\s+){1,4}"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct|Place|Pl|Marg|Nagar)\b"
)

# A date, or a span of dates, is a run of digits too and must never be taken for a phone number: "2021-03 - 2023-08",
# "03.2021 - 08.2023", "03-21 - 08-23", "2019 - 2021", "04-03-1998", "202103 - 202308". A phone number's groups (415,
# 5551234, 0132, 90000) are not a year, a month or a day in these shapes, and a leading + never fits them. A span with a
# slash or an en dash is already safe: neither can be inside a candidate of _PHONE_RE, so it splits into short runs.
_Y = r"(?:19|20)\d\d"
_M = r"(?:0?[1-9]|1[0-2])"
_D = r"(?:0?[1-9]|[12]\d|3[01])"
_S = r"[-./ ]"
_DATE = (
    rf"(?:{_Y}(?:{_S}{_M}(?:{_S}{_D})?)?"  # 2021, 2021-03, 2021-03-15
    rf"|{_M}{_S}(?:{_Y}|\d\d)"  # 03-2021, 03-21
    rf"|{_D}{_S}{_M}{_S}(?:{_Y}|\d\d)"  # 15.03.2021
    rf"|{_Y}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?)"  # 202103, 20210315
)
_DATE_SPAN = re.compile(rf"{_DATE}(?:\s*-\s*{_DATE})*")
_YEAR_LIST = re.compile(rf"{_Y}(?:(?:\s*-\s*|\s+){_Y})*")  # 2018 2020 2022, 2018-2020 2021-2023
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 7, 15  # E.164 allows 15; fewer than 7 is not a phone number
_LINK_TRAILING = ".,;:!?)]}"  # sentence punctuation that follows a link without being part of it


@dataclass(frozen=True)
class Replacement:
    start: int  # in the original text
    end: int
    placeholder: str


@dataclass(frozen=True)
class Masked:
    text: str
    replacements: tuple[Replacement, ...]  # in text order; the lengths differ from the placeholders', hence the map

    def original_offset(self, offset: int) -> int:
        """An offset into the masked text, as an offset into the original. Inside a placeholder it gives the start of
        what the placeholder replaced."""
        shift = 0
        for r in self.replacements:
            masked_start = r.start - shift
            masked_end = masked_start + len(r.placeholder)
            if offset < masked_start:
                break
            if offset < masked_end:
                return r.start
            shift += (r.end - r.start) - len(r.placeholder)
        return offset + shift


def _is_phone(candidate: str) -> bool:
    digits = sum(c.isdigit() for c in candidate)
    if not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS:
        return False
    core = candidate.replace("\N{NO-BREAK SPACE}", " ").strip(" ()")  # "(2021-03 - 2023-08)" is a date in brackets
    if _DATE_SPAN.fullmatch(core) or _YEAR_LIST.fullmatch(core):
        return False
    # A bare run of fewer than 9 digits is an amount or an id far more often than a phone number.
    return not (core.isdigit() and digits < 9)


def _spans(
    pattern: re.Pattern[str], text: str, placeholder: str, keep: Callable[[str], bool] = lambda _: True
) -> list[Replacement]:
    found = []
    for m in pattern.finditer(text):
        matched = m.group().rstrip(_LINK_TRAILING) if placeholder == URL else m.group()
        if matched and keep(matched):
            found.append(Replacement(m.start(), m.start() + len(matched), placeholder))
    return found


def mask(text: str) -> Masked:
    # Priority order: when matches overlap (a domain inside an email, digits inside a link) the earlier kind wins.
    candidates = (
        _spans(_EMAIL_RE, text, EMAIL)
        + _spans(_URL_RE, text, URL)
        + _spans(_PHONE_RE, text, PHONE, _is_phone)
        + _spans(_ADDRESS_RE, text, ADDRESS)
    )
    taken: list[Replacement] = []
    for c in candidates:
        if all(c.end <= t.start or c.start >= t.end for t in taken):
            taken.append(c)
    taken.sort(key=lambda r: r.start)
    pieces, cursor = [], 0
    for r in taken:
        pieces += [text[cursor : r.start], r.placeholder]
        cursor = r.end
    pieces.append(text[cursor:])
    return Masked("".join(pieces), tuple(taken))
