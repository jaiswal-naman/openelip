"""Checks the model's answers against the resume text, in code (AI_SYSTEM 2.3 layer 3 and 4.2).

The model only points: which lines, and the names and dates as they are written there. This module copies the statement
and the quote from the cited text, finds each name in that text, parses each date and checks that it runs forward. An
item that fails is handed back with a reason and never kept (T-3-11). The reasons:

    bad_lines, statement_too_long, bad_parent, parent_not_kept, role_without_details, education_without_details,
    name_missing, employer_not_in_text, title_not_in_text, institution_not_in_text, degree_not_in_text,
    name_not_in_text, date_not_in_text, date_unparseable, dates_backwards

An item whose cited text states a protected characteristic is dropped and only counted (REC-07). Nothing here logs.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from app.profile.protected import screen

from .schemas import CandidateFact, EducationSkillItem, RoleItem, StatementItem, UnverifiedItem

if TYPE_CHECKING:
    from app.profile.resume.reader import ReadResult

MAX_STATEMENT = 2000  # profile_facts.statement is checked to 1..2000 characters (DATA_MODEL 3.4)
YEARS = range(1950, 2101)  # numeric sanity: a year outside this is a typo, not a date
STILL_GOING = 10**9  # the end of a current role, as a month count: later than any real date
_MONTHS = "january february march april may june july august september october november december".split()
_DATE = re.compile(
    r"(?:(?P<y1>\d{4})[-/.](?P<m1>\d{1,2})"  # 2021-03, 2021/3
    r"|(?P<m2>\d{1,2})[-/.](?P<y2>\d{4})"  # 03/2021
    r"|(?P<name>[A-Za-z]{3,9})\.?\s+(?P<y3>\d{4})"  # Mar 2021, March 2021, Sept. 2021
    r"|(?P<y4>\d{4}))"  # 2021
)
_STILL = re.compile(r"(?<!\w)(?:present|current|now|ongoing|today)(?!\w)", re.IGNORECASE)

YearMonth = tuple[int, int | None]


@dataclass(frozen=True)
class Checked:
    """What one call's answer came to."""

    kept: list[CandidateFact]
    unverified: list[UnverifiedItem]
    protected_skipped: int


@dataclass
class _Draft:
    kind: str
    span: tuple[int, int]
    quote: str  # the statement as well
    attributes: dict[str, Any]
    parent: int | None = None  # position of the parent in the model's items
    window: tuple[int, int] | None = None  # first and last month of a role, to find overlaps


class _Dates(NamedTuple):
    start: YearMonth | None
    end: YearMonth | None
    current: bool


class _Resume:
    """The cleaned text and its numbered lines, for citing."""

    def __init__(self, result: "ReadResult") -> None:
        self._text = result.text
        self._lines = {line.n: line for line in result.lines}

    def cite(self, first: int, last: int) -> tuple[tuple[int, int], str] | None:
        """The span and the text of lines first..last (both included), or None when they are not all there."""
        start, end = self._lines.get(first), self._lines.get(last)
        if start is None or end is None or first > last:
            return None
        span = (start.start, end.end)
        quote = self._text[span[0] : span[1]]
        return (span, quote) if quote.strip() else None


def _given(value: str | None) -> str | None:
    return (value or "").strip() or None


def _clean(written: str) -> str:
    return " ".join(written.split())


def _search(needle: str, text: str) -> "re.Match[str] | None":
    """needle as it is written in text: whole words, any case, any run of whitespace between them."""
    words = needle.split()
    if not words:
        return None
    return re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)", text, re.IGNORECASE)


def _parse_date(written: str) -> YearMonth | None:
    found = _DATE.fullmatch(written.strip())
    if found is None:
        return None
    year = int(next(g for g in (found["y1"], found["y2"], found["y3"], found["y4"]) if g))
    month: int | None = None
    if found["m1"] or found["m2"]:
        month = int(found["m1"] or found["m2"])
    elif found["name"]:
        word = found["name"].lower()
        month = next((n for n, full in enumerate(_MONTHS, 1) if len(word) >= 3 and full.startswith(word)), None)
        if month is None:
            return None
    if year not in YEARS or (month is not None and not 1 <= month <= 12):
        return None
    return year, month


def _month_count(date: YearMonth, *, early: bool) -> int:
    """Months since year 0. A date with no month is the first month of its year at a start, the last at an end."""
    return date[0] * 12 + (date[1] or (1 if early else 12))


def _iso(date: YearMonth) -> str:
    return f"{date[0]:04d}-{date[1]:02d}" if date[1] else f"{date[0]:04d}"


def _read_dates(start: str | None, end: str | None, quote: str) -> _Dates | str:
    """Each date must be written in the cited text and parse, and the two must run forward. "Present" ends a current
    role. Returns the dates, or the reason they fail."""
    parsed: list[YearMonth | None] = []
    current = False
    for position, value in enumerate((_given(start), _given(end))):
        if value is None:
            parsed.append(None)
            continue
        found = _search(value, quote)
        if found is None:
            return "date_not_in_text"
        written = _clean(found.group())
        if position == 1 and _STILL.fullmatch(written):
            current = True
            parsed.append(None)
            continue
        date = _parse_date(written)
        if date is None:
            return "date_unparseable"
        parsed.append(date)
    first, last = parsed
    if first and last and _month_count(first, early=True) > _month_count(last, early=False):
        return "dates_backwards"
    return _Dates(first, last, current)


def _names(item: Any, fields: tuple[tuple[str, str], ...], quote: str) -> tuple[dict[str, str], str | None]:
    """Each named field that is given must be written in the cited text. Returns it as written there, or the reason."""
    found: dict[str, str] = {}
    for field, reason in fields:
        value = _given(getattr(item, field))
        if value is None:
            continue
        match = _search(value, quote)
        if match is None:
            return {}, reason
        found[field] = _clean(match.group())
    return found, None


def _dated(item: Any, quote: str, attributes: dict[str, Any], *, claims_current: bool = False) -> _Dates | str:
    """Adds the verified dates, the computed duration and the current flag to attributes. Durations never come from the
    model: they are counted here, in whole months, both ends included."""
    dates = _read_dates(item.start_date, item.end_date, quote)
    if isinstance(dates, str):
        return dates
    if dates.start:
        attributes["start_date"] = _iso(dates.start)
    if dates.end:
        attributes["end_date"] = _iso(dates.end)
    if dates.start and dates.end:
        attributes["duration_months"] = _month_count(dates.end, early=False) - _month_count(dates.start, early=True) + 1
    if dates.current or (claims_current and not dates.end and _STILL.search(quote)):
        attributes["is_current"] = True
    return dates


def _role(item: RoleItem, span: tuple[int, int], quote: str) -> _Draft | str:
    if item.kind == "achievement":  # nothing to verify beyond the lines themselves
        return _Draft("achievement", span, quote, {}, item.parent)
    attributes, reason = _names(item, (("employer", "employer_not_in_text"), ("title", "title_not_in_text")), quote)
    if reason:
        return reason
    if item.kind == "role" and not attributes:
        return "role_without_details"
    dates = _dated(item, quote, attributes, claims_current=item.is_current is True)
    if isinstance(dates, str):
        return dates
    draft = _Draft(item.kind, span, quote, attributes, item.parent if item.kind == "project" else None)
    last = _month_count(dates.end, early=False) if dates.end else STILL_GOING if "is_current" in attributes else None
    if dates.start and last is not None:
        draft.window = (_month_count(dates.start, early=True), last)
    return draft


def _education_skill(item: EducationSkillItem, span: tuple[int, int], quote: str) -> _Draft | str:
    if item.kind == "education":
        fields = (("institution", "institution_not_in_text"), ("degree", "degree_not_in_text"))
        attributes, reason = _names(item, fields, quote)
        if reason or not attributes:
            return reason or "education_without_details"
    else:
        name = _given(item.name)
        if name is None:
            return "name_missing"
        match = _search(name, quote)
        if match is None:
            return "name_not_in_text"
        if item.kind == "skill":  # one fact per skill: its own words, not the whole line
            span = (span[0] + match.start(), span[0] + match.end())
            return _Draft("skill", span, quote[match.start() : match.end()], {"name": _clean(match.group())})
        attributes = {"name": _clean(match.group())}
    dates = _dated(item, quote, attributes)
    return dates if isinstance(dates, str) else _Draft(item.kind, span, quote, attributes)


def _statement(item: StatementItem, span: tuple[int, int], quote: str) -> _Draft:
    return _Draft("other", span, quote, {"section": "summary"})


def _flag_overlaps(drafts: list[_Draft]) -> None:
    """A role whose months overlap another role's is flagged for the candidate to look at, never dropped. ponytail:
    a year with no month counts as the whole year, so roles that merely touch in a year can be flagged."""
    dated = [(d, d.window) for d in drafts if d.window]
    for one, (first, last) in dated:
        if any(other is not one and first < end and begin < last for other, (begin, end) in dated):
            one.attributes["flags"] = ["overlaps_other_role"]


def _check(
    items: list[Any],
    result: "ReadResult",
    build: Callable[[Any, tuple[int, int], str], _Draft | str],
    annotate: Callable[[list[_Draft]], None] | None = None,
) -> Checked:
    doc = _Resume(result)
    drafts: dict[int, _Draft] = {}
    unverified: list[UnverifiedItem] = []
    skipped = 0
    for position, item in enumerate(items):
        kind = getattr(item, "kind", "other")
        cited = doc.cite(item.line_start, item.line_end)
        if cited is None:
            unverified.append(UnverifiedItem(kind, "bad_lines", None, None))
            continue
        span, quote = cited
        if screen(quote):  # a protected characteristic never becomes a fact, and is not shown as one that failed
            skipped += 1
            continue
        built = build(item, span, quote) if len(quote) <= MAX_STATEMENT else "statement_too_long"
        if isinstance(built, str):
            unverified.append(UnverifiedItem(kind, built, span, quote))
        else:
            drafts[position] = built
    for position, draft in list(drafts.items()):  # a child whose role was not kept has no verified place
        parent = draft.parent
        if parent is None:
            continue
        reason = None
        if not 0 <= parent < len(items) or getattr(items[parent], "kind", "") != "role":
            reason = "bad_parent"
        elif parent not in drafts:
            reason = "parent_not_kept"
        if reason:
            drafts.pop(position)
            unverified.append(UnverifiedItem(draft.kind, reason, draft.span, draft.quote))
    placed: dict[int, int] = {}  # item position -> position in the kept list; an exact duplicate shares its twin's
    unique: list[_Draft] = []
    first_seen: dict[tuple[str, tuple[int, int], str], int] = {}
    for position, draft in drafts.items():
        key = (draft.kind, draft.span, json.dumps(draft.attributes, sort_keys=True))
        if key not in first_seen:
            first_seen[key] = position
            placed[position] = len(unique)
            unique.append(draft)
        else:
            placed[position] = placed[first_seen[key]]
    if annotate:
        annotate(unique)
    kept = [
        CandidateFact(d.kind, d.quote, d.attributes, None if d.parent is None else placed[d.parent], d.span, d.quote)
        for d in unique
    ]
    return Checked(kept, unverified, skipped)


def check_roles(items: list[RoleItem], result: "ReadResult") -> Checked:
    return _check(items, result, _role, _flag_overlaps)


def check_education_skills(items: list[EducationSkillItem], result: "ReadResult") -> Checked:
    return _check(items, result, _education_skill)


def check_statements(items: list[StatementItem], result: "ReadResult") -> Checked:
    return _check(items, result, _statement)
