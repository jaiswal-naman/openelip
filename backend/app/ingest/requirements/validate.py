"""Turn the model's cited items into stored rows (AI_SYSTEM 4.3, invariant 2).

The model supplies only segment numbers, a quote to find and two enums. Text and span are computed here from the
posting, so a row can never hold words the posting does not contain. The database repeats the check on insert.
"""

import re
from collections import Counter
from dataclasses import dataclass

from .schema import Importance, Kind, RequirementItem, RequirementList
from .segment import Segment

MAX_REQUIREMENTS = 30  # AI_SYSTEM 4.3 (assumption)
MAX_YEARS = 60  # job_requirements.min_years CHECK (DATA_MODEL 3.5)
_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class RequirementRow:
    ordinal: int  # 1..n in the order of the posting
    importance: Importance
    kind: Kind
    text: str  # always description[start:end]
    start: int
    end: int
    min_years: int | None


@dataclass(frozen=True)
class ValidationResult:
    rows: list[RequirementRow]
    rejected: dict[str, int]  # rule code -> items; min_years_dropped counts kept rows that lost an unwritten number


def _span(description: str, by_number: dict[int, Segment], item: RequirementItem) -> tuple[int, int] | str:
    """The (start, end) an item cites, or the code of the rule it breaks."""
    cited = sorted(set(item.segments))
    if not cited or any(n not in by_number for n in cited):
        return "segment_missing"
    if cited[-1] - cited[0] != len(cited) - 1:  # distinct whole numbers are adjacent when they fill their range
        return "segments_not_adjacent"
    first, last = by_number[cited[0]], by_number[cited[-1]]
    quote = (item.quote or "").strip()  # a blank quote says nothing, like null
    if not quote:
        return first.start, last.end
    at = description.find(quote, first.start, last.end)  # exact, case-sensitive, and only inside the cited lines
    return (at, at + len(quote)) if at >= 0 else "quote_not_in_segments"


def _is_written(years: int, text: str) -> bool:
    """The number must be in range and appear in the text as a whole number (10 does not contain 1).

    Compared as strings: a posting can hold a digit run longer than int() accepts."""
    if not 0 <= years <= MAX_YEARS:
        return False
    return str(years) in {number.lstrip("0") or "0" for number in _NUMBER.findall(text)}


def to_rows(description: str, segments: list[Segment], output: RequirementList) -> ValidationResult:
    by_number = {s.n: s for s in segments}
    rejected: Counter[str] = Counter()
    first_for_span: dict[tuple[int, int], RequirementItem] = {}
    for item in output.requirements:
        span = _span(description, by_number, item)
        if isinstance(span, str):
            rejected[span] += 1
        elif span in first_for_span:
            rejected["duplicate"] += 1  # the same words cited twice: the first item stands
        else:
            first_for_span[span] = item
    kept = sorted(first_for_span.items(), key=lambda pair: pair[0])  # by position in the posting, not answer order
    if len(kept) > MAX_REQUIREMENTS:
        rejected["over_cap"] = len(kept) - MAX_REQUIREMENTS
        kept = kept[:MAX_REQUIREMENTS]
    rows: list[RequirementRow] = []
    for ordinal, ((start, end), item) in enumerate(kept, start=1):
        text = description[start:end]
        years = item.min_years
        if years is not None and not _is_written(years, text):
            rejected["min_years_dropped"] += 1
            years = None
        rows.append(RequirementRow(ordinal, item.importance, item.kind, text, start, end, years))
    return ValidationResult(rows, dict(rejected))


def output_acceptable(result: ValidationResult) -> bool:
    """False only when the model proposed items and none survived: the one answer worth a fresh call (AI_SYSTEM 2.4).

    A partly valid answer keeps its valid items, and an empty answer is a posting that states no requirements."""
    return bool(result.rows) or not result.rejected
