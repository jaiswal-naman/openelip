"""What the campaign crew's roles share: alias maps for model requests, and the test for a sentence that needs a source.

Aliases keep ids and names out of a request (AI_SYSTEM 5): the model sees f1..fN for facts, r1..rM for requirements,
d1..dK for dossier claims and e1..eJ for campaign events, and an answer that cites any other alias is removed.
"""

import re
from collections.abc import Iterable, Sequence
from typing import TypeVar

from app.tailor import api as tailor

T = TypeVar("T")

# What a courteous follow-up may say with no source (INDEX decision 14). The words are given to the check below as if a
# fact held them, so a greeting, thanks or a plain statement of interest has nothing "new" in it.
COURTESY = (
    "Thank you for your time and consideration. I am writing to follow up on my application and I remain interested "
    "in the role and the team. I would be glad to answer any questions and I look forward to hearing from you."
)

_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")
# assumption: words that put pay, notice or availability into a message. The candidate states these, never a model.
_RESTRICTED_WORDS = re.compile(
    r"\b(salary|salaries|pay|compensation|rate|remuneration|notice\s+period|notice|start\s+date|availab\w*|"
    r"visa|sponsorship|work\s+authori[sz]ation|authori[sz]ed\s+to\s+work)\b",
    re.IGNORECASE,
)
_CURRENCY = re.compile(r"[$€£]\s?\d|\d\s?(?:usd|eur|gbp|k\b)", re.IGNORECASE)


def aliases(prefix: str, items: Iterable[T]) -> dict[str, T]:
    """items numbered prefix1..prefixN in the order given. Callers pass them in a stable order (created_at, then id)."""
    return {f"{prefix}{number}": item for number, item in enumerate(items, 1)}


def resolve(names: Sequence[str], table: dict[str, T]) -> list[T] | None:
    """The items the aliases name, or None when any alias is unknown (the whole citation then fails)."""
    found = [table.get(name) for name in names]
    return None if any(item is None for item in found) else [item for item in found if item is not None]


def needs_citation(text: str, *, skills: Iterable[str], job_terms: Iterable[str]) -> bool:
    """Does this sentence state something checkable (a number, date, skill, title, employer or other name)? Greetings,
    thanks and plain interest do not (decision 14). It is layer 1 of the tailoring verifier run against a stand-in fact
    that holds only courtesy words, so anything the courtesy words do not hold counts as new."""
    stand_in = tailor.Fact(id="courtesy", kind="note", statement=COURTESY)
    return tailor.unsupported_reason(text, [stand_in], skills=skills, job_terms=job_terms) is not None


def restricted_reason(text: str) -> str | None:
    """A reason code when the sentence carries contact details or states pay, notice or availability, else None. Code
    writes the recipient and the sign-off; the model writes none of these."""
    if _EMAIL.search(text):
        return "contact_email"
    if _LINK.search(text):
        return "contact_link"
    if _PHONE.search(text):
        return "contact_phone"
    if _RESTRICTED_WORDS.search(text) or _CURRENCY.search(text):
        return "restricted_content"
    return None


def count(removed: dict[str, int], reason: str) -> None:
    removed[reason] = removed.get(reason, 0) + 1
