"""Restricted data: pay and ways to reach a person (AI_SYSTEM 8.3, 8.6; INDEX "Restricted patterns").

One list for the whole system. The Twin runs it before any model call and again on every sentence it would send, and
the Guardian's `scope` check for `twin_answer` and the reply pre-check call it too. It is deliberately over-eager: a
false positive sends a question to the candidate, a miss would leak pay or contact details, so the patterns fail closed.

Pure code, no database. The word lists are assumptions (AI_SYSTEM 8.3: "a fixture list with its own tests"); the fixture
list is backend/tests/twin/fixtures/logic/restricted_cases.json.
"""

import re
import unicodedata
from dataclasses import dataclass

PHONE, EMAIL, ADDRESS, SOCIAL, PAY = "phone", "email", "address", "social_profile", "pay"
CONTACT_DETAILS, CURRENT_PAY = "contact_details", "current_pay"
CONTACT_KINDS = (PHONE, EMAIL, ADDRESS, SOCIAL)
TOPIC_OF = {
    PHONE: CONTACT_DETAILS,
    EMAIL: CONTACT_DETAILS,
    ADDRESS: CONTACT_DETAILS,
    SOCIAL: CONTACT_DETAILS,
    PAY: CURRENT_PAY,
}


@dataclass(frozen=True)
class Restricted:
    kind: str  # phone | email | address | social_profile | pay
    topic: str  # contact_details | current_pay


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))

# assumption: AI_SYSTEM 8.3 fixture list
_PAY = re.compile(
    r"\b(?:salary|salaries|salaried|compensation|remuneration|ctc|lpa|wages?|paycheck|paycheque|payslips?|"
    r"pay|paid|payout|earn(?:s|ed|ing|ings)?|income|take[\s-]?home|in[\s-]?hand|"
    r"(?:pay|salary|compensation|benefits|annual|total|current|ctc)\s+package|package\s+(?:details|expectations?)|"
    r"lakhs?|per\s+annum|per\s+hour|hourly\s+rate|day\s+rate|how\s+much\s+(?:does|do|did|is|are|was)\s+\w+\s+"
    r"(?:make|making|get|getting|cost|charge))\b"
    r"|[$€£₹]\s?\d|\d\s?(?:usd|eur|gbp|inr|rupees|dollars)\b",
    re.IGNORECASE,
)

# assumption: AI_SYSTEM 8.3 fixture list
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_EMAIL_SPOKEN = re.compile(
    r"\b\w+(?:\s+dot\s+\w+)*\s+at\s+\w+(?:\s+dot\s+\w+)+\b"
    r"|\b\w+\s*[\[(]\s*at\s*[\])]\s*\w+(?:\s*[\[(]\s*dot\s*[\])]\s*\w+)+",
    re.IGNORECASE,
)

_PHONE_DIGITS = re.compile(r"(?<![\w.])\+?\(?\d[\d\s().\-]{5,}\d")
_YEAR_RANGE = re.compile(r"\s*(?:19|20)\d{2}\s*[-–]\s*(?:19|20)\d{2}\s*")
_ISO_DATE = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s*")
_DIGIT_WORDS = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_PHONE_SPOKEN = re.compile(rf"\b{_DIGIT_WORDS}(?:[\s,.-]+{_DIGIT_WORDS}){{6,}}\b", re.IGNORECASE)
PHONE_MIN_DIGITS = 7  # assumption: shortest local number
PHONE_MAX_DIGITS = 15  # E.164

_TIME_UNITS = r"(?:years?|yrs?|months?|weeks?|days?|hours?|hrs?|minutes?|mins?|people|engineers?|members?|times|x)\b"
_STREET_WORDS = (
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|boulevard|blvd|court|crescent|terrace|nagar|marg|colony|sector)"
)
# assumption: a number, up to four words, a street word; "5 years on the road" is excluded by the unit lookahead
_ADDRESS = re.compile(
    rf"(?<![\w.])\d{{1,5}}[A-Za-z]?(?![\d]|\s+{_TIME_UNITS})(?:\s+[\w.'-]+){{0,4}}?\s+{_STREET_WORDS}\b"
    r"|\bp\.?\s?o\.?\s+box\s+\d+|\b(?:apt|apartment|flat|suite)\.?\s+#?\d+[a-z]?\b",
    re.IGNORECASE,
)

_NETWORKS = (
    r"linkedin|github|gitlab|twitter|facebook|instagram|telegram|whatsapp|skype|discord|signal|snapchat|tiktok|"
    r"mastodon|bluesky|behance|dribbble|stackoverflow|kaggle|leetcode|codeforces|medium"
)
_SOCIAL = re.compile(
    rf"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)?(?:{_NETWORKS}|x)\.(?:com|org|io|me|net|social)/\S*|\b(?:t|wa)\.me/\S*"
    rf"|\b(?:{_NETWORKS})\s+(?:profile|handle|id|username|user\s?name|account|url|link|page|number|dm|me)\b"
    rf"|\b(?:her|his|their|your|the\s+candidate'?s?)\s+(?:{_NETWORKS})\b"
    r"|(?<![\w@.])@[A-Za-z0-9_]{3,}\b",
    re.IGNORECASE,
)

_SPACED_LETTERS = re.compile(r"\b(?:\w[\s.\-_*]){3,}\w\b")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def _phone_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for found in _PHONE_DIGITS.finditer(text):
        run = found.group()
        digits = sum(c.isdigit() for c in run)
        if not PHONE_MIN_DIGITS <= digits <= PHONE_MAX_DIGITS:
            continue
        if _YEAR_RANGE.fullmatch(run) or _ISO_DATE.fullmatch(run):
            continue
        spans.append((found.start(), found.end()))
    spans += [(m.start(), m.end()) for m in _PHONE_SPOKEN.finditer(text)]
    return spans


def _contact_spans(text: str) -> list[tuple[str, int, int]]:
    """(kind, start, end) of every contact detail in already-normalised text."""
    spans = [(PHONE, s, e) for s, e in _phone_spans(text)]
    spans += [(EMAIL, m.start(), m.end()) for m in (*_EMAIL.finditer(text), *_EMAIL_SPOKEN.finditer(text))]
    spans += [(ADDRESS, m.start(), m.end()) for m in _ADDRESS.finditer(text)]
    spans += [(SOCIAL, m.start(), m.end()) for m in _SOCIAL.finditer(text)]
    return sorted(spans, key=lambda s: s[1])


def _collapse_spaced(text: str) -> str:
    """ "s.a.l.a.r.y" and "s a l a r y" read as the word, so letter-spacing is no way round the pay list."""
    return _SPACED_LETTERS.sub(lambda m: re.sub(r"[\s.\-_*]", "", m.group()), text)


def restricted_matches(text: str) -> list[Restricted]:
    """Every kind of restricted data the text holds, once each, in the order phone, email, address, social_profile, pay.
    Case-insensitive; Unicode is normalised (NFKC) and zero-width characters are dropped first."""
    clean = _normalise(text)
    kinds = {kind for kind, _, _ in _contact_spans(clean)}
    if _PAY.search(clean) or _PAY.search(_collapse_spaced(clean)):
        kinds.add(PAY)
    return [Restricted(kind, TOPIC_OF[kind]) for kind in (*CONTACT_KINDS, PAY) if kind in kinds]


def redact_contact(text: str, placeholder: str = "[removed]") -> str:
    """The text with phone numbers, email addresses, street addresses and profile links replaced. Pay words stay: pay is
    never given to the Twin as a topic, and a fact that only mentions it is refused at validation instead."""
    clean = _normalise(text)
    out, at = [], 0
    for _, start, end in _contact_spans(clean):
        if start < at:  # overlapping match: the earlier span already covered it
            at = max(at, end)
            continue
        out += [clean[at:start], placeholder]
        at = end
    out.append(clean[at:])
    return "".join(out)
