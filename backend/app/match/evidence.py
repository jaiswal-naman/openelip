"""The task match.evidence.score (AI_SYSTEM 4.4): the request for one candidate and one job, and the call that gets the
verdicts. app.match.validator judges the answer; this module builds what is sent and sends it, nothing more.

The request is de-identified (data class de-identified, AI_SYSTEM 2.6): facts and requirements travel under aliases
(f1..fN, r1..rM), never under ids. A fact is sent as its kind and statement with contact details, links and the person's
name replaced by "[removed]"; no document, span, date added or photo goes with it. Role facts carry their length and
the request carries the total career length, counted here by code (a model's date arithmetic is not trusted, and rule 5
of the validator holds the answer to the same total). The posting's requirement texts sit between two marker lines the
prompt names as data, each on one line, so a posting cannot start a line of its own.

The inputs are the plain dicts of the other modules' api files: profile.api.usable_facts rows (id, kind, statement,
attributes, source, created_at, provenance {document_id, span {start, end}, quote}) and ingest.api.get_job's
"requirements" (id, ordinal, text, importance, kind, span {start, end}, min_years).
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from app.platform.llm import complete

from .validator import VerdictSet

TASK = "match.evidence.score"
PROMPT = (Path(__file__).parent / "prompts" / "evidence_score.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)

REMOVED = "[removed]"
MONTHS_PER_YEAR = 12
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 7, 15  # E.164 allows 15; fewer than 7 is not a phone number
NO_SPAN = 2_147_483_647  # sorts a fact with no source span after the ones that have one, as profile.api does

# Best-effort second guard: Phase 3 masks contact details in resume lines before any fact exists, so these only catch
# what a fact's own words still carry. Kept local because match may not import profile internals.
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# A scheme or www. prefix, or a host on a common top-level domain written in lower case. An upper-case domain is left
# alone on purpose: ASP.NET is a skill, not a link.
_LINK = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|org|net|dev|me|io|co|in|ai|app|xyz|site|tech)\b(?:/[^\s<>\"']*)?"
)
_LINK_TRAILING = ".,;:!?)]}"  # sentence punctuation that follows a link without being part of it
_PHONE = re.compile(r"(?<!\w)(?:\+ ?)?\(?\d[\d \u00a0().-]{5,}\d(?!\w)")  # _is_phone() then decides
_DATE_PART = re.compile(r"(?:19|20)\d\d|\d{1,2}")  # a year, a month or a day
_DATE = re.compile(r"(\d{4})(?:-(\d{1,2}))?")  # attributes.start_date and end_date: YYYY or YYYY-MM


@dataclass(frozen=True)
class FactRef:
    """What the validator needs to know about a fact the request carried."""

    id: str  # profile_facts.id
    kind: str
    statement: str  # as sent
    span: tuple[int, int] | None  # source span in the resume text; None for a fact with no document
    confirmed_by_event: bool  # a fact that did not come from a resume was confirmed by a profile event (DATA_MODEL 3.4)


@dataclass(frozen=True)
class RequirementRef:
    id: int  # job_requirements.id
    text: str
    importance: str
    kind: str
    min_years: int | None
    span: tuple[int, int]  # code points in the posting
    at_span: str | None  # the posting's text at that span when the request was built; None when the span is outside it


@dataclass(frozen=True)
class EvidenceRequest:
    messages: list[dict[str, str]]
    facts: dict[str, FactRef]  # alias -> fact, f1..fN in (created_at, source span, id) order
    requirements: dict[str, RequirementRef]  # alias -> requirement, r1..rM by ordinal
    career_months: int  # the union of the role date ranges, 0 when no role has dates


def _remove_link(found: "re.Match[str]") -> str:
    link = found.group()
    return REMOVED + link[len(link.rstrip(_LINK_TRAILING)) :]


def _is_phone(candidate: str) -> bool:
    groups = re.findall(r"\d+", candidate)
    digits = sum(len(group) for group in groups)
    if not MIN_PHONE_DIGITS <= digits <= MAX_PHONE_DIGITS:
        return False
    if all(_DATE_PART.fullmatch(group) for group in groups):  # 2013 - 2017, 2019-01 - 2022-03
        return False
    return not (len(groups) == 1 and digits < 9)  # a bare run of fewer than 9 digits is an amount or an id


def _clean(statement: str, names: Sequence[str]) -> str:
    """The statement on one line, with an email, a link, a phone number and the person's name (whole and by part)
    replaced by [removed]."""
    flat = _LINK.sub(_remove_link, _EMAIL.sub(REMOVED, statement))
    flat = _PHONE.sub(lambda found: REMOVED if _is_phone(found.group()) else found.group(), flat)
    words = sorted({w for name in names for w in (name.strip(), *name.split()) if len(w) >= 3}, key=len, reverse=True)
    if words:
        flat = re.sub(rf"(?<!\w)(?:{'|'.join(map(re.escape, words))})(?!\w)", REMOVED, flat, flags=re.IGNORECASE)
    return " ".join(flat.split())


def _month_index(value: Any, *, start: bool) -> int | None:
    """Months since year 0 of "2019" or "2019-03". A date with no month is the first month of its year at a start and
    the last at an end, as the resume reader counts it."""
    found = _DATE.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return None
    month = int(found.group(2)) if found.group(2) else (1 if start else 12)
    return int(found.group(1)) * MONTHS_PER_YEAR + month if 1 <= month <= 12 else None


def _role_window(fact: Mapping[str, Any], today: date) -> tuple[int, int] | None:
    """The first and last month of a role (both counted), or None when the role has no usable dates. A current role
    runs to today; no end is ever guessed otherwise, and no role runs past today."""
    if fact["kind"] != "role":
        return None
    attributes = fact.get("attributes") or {}
    first = _month_index(attributes.get("start_date"), start=True)
    last = _month_index(attributes.get("end_date"), start=False)
    now = today.year * MONTHS_PER_YEAR + today.month
    if first is None or (last is None and attributes.get("is_current") is not True):
        return None
    last = now if last is None else min(last, now)
    return (first, last) if first <= last else None


def _union_months(windows: list[tuple[int, int]]) -> int:
    """Months covered by any window: overlapping roles are counted once."""
    total, covered_to = 0, None
    for first, last in sorted(windows):
        if covered_to is None or first > covered_to:
            total += last - first + 1
            covered_to = last
        elif last > covered_to:
            total += last - covered_to
            covered_to = last
    return total


def _length(months: int) -> str:
    years, rest = divmod(months, MONTHS_PER_YEAR)
    parts = [f"{n} {word}{'' if n == 1 else 's'}" for n, word in ((years, "year"), (rest, "month")) if n]
    return " ".join(parts)


def _fact_order(fact: Mapping[str, Any]) -> tuple[Any, int, str]:
    provenance = fact.get("provenance")
    return fact["created_at"], provenance["span"]["start"] if provenance else NO_SPAN, str(fact["id"])


def _fact_ref(fact: Mapping[str, Any], names: Sequence[str]) -> FactRef:
    provenance = fact.get("provenance")
    span = (provenance["span"]["start"], provenance["span"]["end"]) if provenance else None
    return FactRef(str(fact["id"]), fact["kind"], _clean(fact["statement"], names), span, fact["source"] != "resume")


def _requirement_ref(requirement: Mapping[str, Any], description: str) -> RequirementRef:
    start, end = requirement["span"]["start"], requirement["span"]["end"]
    return RequirementRef(
        id=requirement["id"],
        text=requirement["text"],
        importance=requirement["importance"],
        kind=requirement["kind"],
        min_years=requirement.get("min_years"),
        span=(start, end),
        at_span=description[start:end] if 0 <= start < end <= len(description) else None,
    )


def build_request(
    facts: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    *,
    description: str,
    names: Sequence[str] = (),
    today: date | None = None,
) -> EvidenceRequest:
    """The messages and the alias maps for one candidate and one job. description is the posting text the requirement
    spans point into (rule 3 of the validator re-reads it); names are the person's names to remove from fact text;
    today ends a current role (default: today in UTC). A caller does not score a job that has no requirements."""
    today = today or datetime.now(timezone.utc).date()
    ordered = sorted(facts, key=_fact_order)
    fact_refs = {f"f{n}": _fact_ref(fact, names) for n, fact in enumerate(ordered, 1)}
    windows = [_role_window(fact, today) for fact in ordered]
    lines = [
        f"[{alias}] {ref.kind}: {ref.statement}" + (f" ({_length(w[1] - w[0] + 1)})" if w else "")
        for (alias, ref), w in zip(fact_refs.items(), windows, strict=True)
    ]
    career = _union_months([w for w in windows if w is not None])
    requirement_refs = {
        f"r{n}": _requirement_ref(requirement, description)
        for n, requirement in enumerate(sorted(requirements, key=lambda r: r["ordinal"]), 1)
    }
    user = "\n".join(
        [
            "Confirmed facts about the candidate:",
            *(lines or ["(none)"]),
            f"Total career length, counted from the dated roles with overlaps counted once: {_length(career)}."
            if career
            else "The career length is not known: no role has dates.",
            "",
            "Requirements of the job:",
            "BEGIN POSTING TEXT",
            *(f"[{alias}] {' '.join(ref.text.split())}" for alias, ref in requirement_refs.items()),
            "END POSTING TEXT",
        ]
    )
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]
    return EvidenceRequest(messages, fact_refs, requirement_refs, career)


def score(request: EvidenceRequest, *, principal: UUID | None) -> VerdictSet:
    """One verdict per requirement, from the gateway. Raises its AIDisabled (no usable route, nothing called),
    ProviderUnavailable (timeout or provider error) and SchemaInvalid (invalid twice). The answer is not yet trusted:
    pass it to validator.validate. principal is the person the call is charged to (None for an evaluation run)."""
    answer = complete(TASK, VerdictSet, request.messages, principal=principal, prompt_version=PROMPT_VERSION)
    return cast(VerdictSet, answer)
