"""Dealbreaker typing (REC-03, REC-04, AI_SYSTEM section 3 row 10).

A candidate states a dealbreaker in words. type_dealbreaker() says whether it fits a hard constraint the matcher can
check against typed job fields and, if so, which one. Anything else returns None and is stored as an untyped soft
preference with a "not guaranteed" notice (REC-04).

It is code, not a model: a small ordered rule table, so the same words always give the same answer. A statement that
fits two rules is compound and returns None, because suggesting half of it would silently drop the other half. The
result is only a suggestion: nothing becomes a hard constraint until the candidate confirms it.

`fields` use the typed columns of DATA_MODEL `constraints`: arrangements, employment_types, salary_floor with
salary_currency (annual, whole currency units), company_names. A currency is never guessed: no currency word, no result.
"""

import re
from dataclasses import dataclass
from typing import Any

ARRANGEMENTS = ("onsite", "hybrid", "remote")  # the work_arrangement enum
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary")  # employment_type minus 'unknown'


@dataclass(frozen=True)
class ConstraintSuggestion:
    kind: str  # a constraint_kind value
    fields: dict[str, Any]


def _suggest(kind: str, **fields: Any) -> ConstraintSuggestion:
    return ConstraintSuggestion(kind, fields)


# --- remote_policy and employment_type -----------------------------------------------------------------------------
# Both follow one shape: the statement names exactly one value ("no contract work", "only remote"). Two values in one
# statement ("no remote or hybrid", "contract or part-time only") are ambiguous, so they are not typed.

_NO = (
    r"\b(?:(?:do\s*not|don'?t|won'?t|will\s+not)\s+want\s+(?:to\s+work\s+)?"
    r"|(?:no|not|never|without|avoid)\s+)(?:any\s+)?"
)
_ONLY = re.compile(r"\bonly\b", re.IGNORECASE)

_ARRANGEMENT_WORD = r"(remote(?:ly)?|hybrid|on-?site|in[- ]office)"
_ARRANGEMENT_OF = {
    "remote": "remote",
    "remotely": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite",
    "inoffice": "onsite",
}
_NOT_ARRANGEMENT = re.compile(_NO + r"(?:fully\s+)?" + _ARRANGEMENT_WORD, re.IGNORECASE)
_FULLY_REMOTE = re.compile(r"\b(?:fully|100%|completely|entirely)\s+remote\b", re.IGNORECASE)

_TYPE_WORD = r"(full[- ]?time|part[- ]?time|contract(?:or|ing)?|internships?|temp(?:orary)?)"
_TYPE_OF = {
    "fulltime": "full_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "contracting": "contract",
    "internship": "internship",
    "internships": "internship",
    "temp": "temporary",
    "temporary": "temporary",
}
_NOT_TYPE = re.compile(_NO + _TYPE_WORD, re.IGNORECASE)


def _distinct(pattern: str, spoken_to_value: dict[str, str], text: str) -> set[str]:
    return {spoken_to_value[re.sub(r"[- ]", "", w.lower())] for w in re.findall(pattern, text, re.IGNORECASE)}


def _remote_policy(text: str) -> ConstraintSuggestion | None:
    mentioned = _distinct(_ARRANGEMENT_WORD, _ARRANGEMENT_OF, text)
    if len(mentioned) != 1:
        return None
    (only,) = mentioned
    if _NOT_ARRANGEMENT.search(text):
        return _suggest("remote_policy", arrangements=[a for a in ARRANGEMENTS if a != only])
    if _ONLY.search(text) or (only == "remote" and _FULLY_REMOTE.search(text)):
        return _suggest("remote_policy", arrangements=[only])
    return None


def _employment_type(text: str) -> ConstraintSuggestion | None:
    mentioned = _distinct(_TYPE_WORD, _TYPE_OF, text)
    if len(mentioned) != 1:
        return None
    (only,) = mentioned
    if _NOT_TYPE.search(text):
        return _suggest("employment_type", employment_types=[t for t in EMPLOYMENT_TYPES if t != only])
    if _ONLY.search(text):
        return _suggest("employment_type", employment_types=[only])
    return None


# --- salary_floor --------------------------------------------------------------------------------------------------

_FLOOR = re.compile(
    r"\b(?:at\s+least|minimum(?:\s+of)?|min\.?|(?:not|no)\s+(?:less|lower)\s+than|(?:nothing|not)\s+(?:below|under)"
    r"|floor\s+of|at\s+or\s+above)\s",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k|mn|m|million|lakhs?|lacs?|lpa|crores?)?\b", re.IGNORECASE)
_SCALE = {
    "k": 10**3,
    "m": 10**6,
    "mn": 10**6,
    "million": 10**6,
    "lakh": 10**5,
    "lakhs": 10**5,
    "lac": 10**5,
    "lacs": 10**5,
    "lpa": 10**5,  # lakhs per annum
    "crore": 10**7,
    "crores": 10**7,
}
_INDIAN_UNITS = frozenset({"lakh", "lakhs", "lac", "lacs", "lpa", "crore", "crores"})  # Indian numbering: rupees
# Only words that name one currency. A bare "$" can be US, Canadian or Australian dollars, so it is not on the list.
_CURRENCY = re.compile(r"\b(?:INR|USD|EUR|GBP|CAD|AUD|SGD|AED)\b|US\$|₹|\bRs\b\.?|\brupees?\b|€|£", re.IGNORECASE)
_CODE_OF = {
    "us$": "USD",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "€": "EUR",
    "£": "GBP",
}
_NOT_ANNUAL = re.compile(
    r"\b(?:per|a|each)\s+(?:month|hour|day|week)\b|\b(?:monthly|hourly|daily|weekly)\b"
    r"|/\s?(?:mo|month|hr|hour|day|week)\b",
    re.IGNORECASE,
)
# Plausible annual floors: below 1,000 in any currency it is a count ("5 years CAD") and above the column's integer
# range it cannot be stored.
_MIN_FLOOR, _MAX_FLOOR = 1_000, 2_147_483_647


def _currency_near(text: str, start: int, end: int) -> str | None:
    """The one currency named just before or just after the amount; None when none or two different ones are."""
    found = {
        _CODE_OF.get(m.group().lower(), m.group().upper())
        for m in _CURRENCY.finditer(text, max(0, start - 6), end + 14)
    }
    return found.pop() if len(found) == 1 else None


def _salary_floor(text: str) -> ConstraintSuggestion | None:
    floor = _FLOOR.search(text)
    amount = floor and _AMOUNT.search(text, floor.end())
    if not amount or _NOT_ANNUAL.search(text):
        return None
    unit = (amount.group(2) or "").lower()
    currency = _currency_near(text, amount.start(), amount.end())
    if unit in _INDIAN_UNITS:
        if currency not in (None, "INR"):  # "30 lakh USD" contradicts itself
            return None
        currency = "INR"
    value = round(float(amount.group(1).replace(",", "")) * _SCALE.get(unit, 1))
    if currency is None or not _MIN_FLOOR <= value <= _MAX_FLOOR:
        return None
    return _suggest("salary_floor", salary_floor=value, salary_currency=currency)


# --- excluded_company ----------------------------------------------------------------------------------------------

_EXCLUDE = re.compile(
    r"(?i:\b(?:not|never|no|avoid|exclude|excluding|except"
    r"|(?:do\s*not|don'?t|won'?t|will\s+not)\s+(?:want\s+to\s+)?(?:work\s+(?:at|for)|apply\s+to|join))\b"
    r"(?:\s+(?:at|for|from|with))?)\s+(?P<names>[A-Z0-9].*)$",
    re.DOTALL,
)
_NAME = re.compile(r"[A-Z0-9][\w&.'-]*(?:\s+(?:(?:of|the|for|de|von|&)\s+)?[A-Z0-9][\w&.'-]*)*")
# A title-cased statement ("No Heavy Travel") is not a company name. Only the first word is checked, so "Acme Travel"
# still is one.
_NOT_A_COMPANY = frozenset(
    (
        "remote hybrid onsite on-site contract contractor internship temporary travel overtime weekend weekends "
        "night nights shift shifts relocation commute politics meetings work job jobs role roles heavy long toxic "
        "low poor"
    ).split()
)


def _excluded_company(text: str) -> ConstraintSuggestion | None:
    found = _EXCLUDE.search(text.strip())
    if not found:
        return None
    names = [n.strip() for n in re.split(r",|;|\bor\b|\band\b", found.group("names").strip().rstrip(".;!"))]
    if not all(_NAME.fullmatch(n) and n.split()[0].lower() not in _NOT_A_COMPANY for n in names):
        return None
    return _suggest("excluded_company", company_names=list(dict.fromkeys(names)))


# --- the table -----------------------------------------------------------------------------------------------------

_RULES = (_salary_floor, _remote_policy, _employment_type, _excluded_company)


def type_dealbreaker(text: str) -> ConstraintSuggestion | None:
    suggestions = [s for rule in _RULES if (s := rule(text))]
    return suggestions[0] if len(suggestions) == 1 else None
