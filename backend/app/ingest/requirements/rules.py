"""A model-free baseline: read a posting's headings and take the lines under the requirement ones (AI_SYSTEM 4.3).

It returns rows of the same shape as the validator's, so the gold set can score it beside the model. It is a baseline
for that comparison, not a product path: with no model key requirements are simply not extracted (ARCHITECTURE 10.4).
"""

import re
from typing import Literal

from .schema import Importance, Kind
from .segment import segments
from .validate import MAX_REQUIREMENTS, MAX_YEARS, RequirementRow

Heading = Literal["required", "preferred", "stop"]  # stop: duties, pay, company text; it ends a requirement list

# A heading is a short line (60 characters, five words) with no digit and no final period.
# It starts with one of these...
_STARTS: dict[Heading, tuple[str, ...]] = {
    "required": (
        "requirement",
        "qualification",
        "required",
        "minimum qualification",
        "minimum requirement",
        "basic qualification",
        "what you'll need",
        "what you need",
        "what you'll bring",
        "what we're looking for",
        "who you are",
    ),
    "preferred": ("preferred", "nice to have", "bonus", "desirable"),
    "stop": (
        "responsibilit",
        "what you'll do",
        "about",
        "benefit",
        "perk",
        "we offer",
        "compensation",
        "salary",
        "how to apply",
        "equal opportunity",
    ),
}
# ...or is one of these and nothing else: as a start they would swallow ordinary lines ("Experience with Go").
_ALONE: dict[str, Heading] = {
    "skills": "required", "experience": "required", "you have": "required", "what you have": "required",
    "must have": "required", "plus": "preferred", "pluses": "preferred",
}  # fmt: skip

_BULLET = re.compile(r"[-*•·▪●◦‣○–—]+\s*")
_SOFT = re.compile(r"\ba plus\b|\bpreferred\b", re.I)
_YEARS = re.compile(r"\b(\d{1,2})\s*(?:\+|-\s*\d{1,2})?[\s-]*(?:years?|yrs?)\b", re.I)  # "5+ years", "3-5 years"
_EDUCATION = re.compile(r"\b(?:degree|bachelor|master[’']?s|ph\.?d|doctorate|diploma)\b", re.I)
_AUTHORIZATION = re.compile(
    r"\bauthori[sz](?:ed|ation) to work\b|\bwork (?:authori[sz]ation|permit|visa)\b|\b(?:right|eligible) to work\b",
    re.I,
)


def _heading(text: str) -> Heading | None:
    low = text.lower().replace("’", "'").rstrip(":").strip()
    if len(text) > 60 or text.endswith(".") or len(low.split()) > 5 or any(c.isdigit() for c in low):
        return None
    if low in _ALONE:
        return _ALONE[low]
    return next((kind for kind, starts in _STARTS.items() if low.startswith(starts)), None)


def _years(text: str) -> int | None:
    found = _YEARS.search(text)
    years = int(found.group(1)) if found else None
    return years if years is not None and years <= MAX_YEARS else None  # the column holds 0 to 60


def _kind(text: str, years: int | None) -> Kind:
    if years is not None:
        return "experience"
    if _EDUCATION.search(text):
        return "education"
    if _AUTHORIZATION.search(text):
        return "authorization"
    return "skill"


def extract(description: str) -> list[RequirementRow]:
    rows: list[RequirementRow] = []
    section: Importance | None = None  # set while inside a required or preferred section
    for segment in segments(description):
        mark = _BULLET.match(segment.text)
        start = segment.start + (mark.end() if mark else 0)
        text = description[start : segment.end]
        if not text:
            continue  # a line of bullet marks only
        heading = _heading(text)
        if heading:
            section = None if heading == "stop" else heading
        elif section:
            years = _years(text)
            importance: Importance = "preferred" if section == "preferred" or _SOFT.search(text) else "required"
            rows.append(RequirementRow(len(rows) + 1, importance, _kind(text, years), text, start, segment.end, years))
    return rows[:MAX_REQUIREMENTS]  # the same cap, by position, as the validator's
