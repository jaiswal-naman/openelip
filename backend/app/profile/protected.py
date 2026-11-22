"""Protected-characteristic screen (REC-07, AI_SYSTEM section 3 row 10).

screen(text) names the category of a protected characteristic the text states, or returns None. It returns the category
only, never the matched text, so a caller has nothing to store or log. Plan 03-03 uses it to decline turning a resume
line into a fact; plan 03-04 uses it to refuse candidate-typed text. Raw resume text is not touched (owner decision G4).

Phrases are label-style on purpose ("Age: 27", "Religion: ..."): a bare word such as "age" or "gender" is far more often
a job-title or skill word ("Chief Age Officer tooling", "gender pay gap dashboard"). Work authorization ("Authorized to
work in India") is a hard-constraint kind and is deliberately not screened.

ponytail: a model-free pattern screen is the v1 ceiling. It misses a characteristic worded as a sentence ("my faith
is ..."); a model check can be added in front of this table later, never in place of it.
"""

import re

_LABEL = r"\s*[:=\u2013\u2014-]"  # "Age: 27", "Age - 27", "Age = 27"

# Ordered: the first row that matches names the category. Case-insensitive.
_TABLE: tuple[tuple[str, str], ...] = (
    ("age", rf"\bdate\s+of\s+birth\b|\bbirth\s*date\b|\bd\.?o\.?b\.?(?:{_LABEL}|\s+\d)|\bage{_LABEL}\s*\d"),
    ("marital_status", r"\bmarital\s+status\b"),
    ("religion", rf"\breligion{_LABEL}"),
    ("gender", rf"\b(?:gender|sex){_LABEL}\s*\w"),
    ("sexual_orientation", r"\bsexual\s+orientation\b"),
    ("nationality", rf"\bnationality{_LABEL}"),
    ("ethnicity", rf"\b(?:race|ethnicity|ethnic\s+origin){_LABEL}"),
    ("caste", r"\bcaste\b"),
    ("disability", r"\bdisabilit(?:y|ies)\b"),
    ("pregnancy", r"\bpregnan(?:t|cy)\b"),
)
_COMPILED = tuple((category, re.compile(pattern, re.IGNORECASE)) for category, pattern in _TABLE)

CATEGORIES = tuple(category for category, _ in _TABLE)


def screen(text: str) -> str | None:
    for category, pattern in _COMPILED:
        if pattern.search(text):
            return category
    return None
