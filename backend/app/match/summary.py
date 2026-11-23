"""The one-line summary of a match (AI_SYSTEM 4.6), rendered by code from the evidence rows. No model: the brief.summary
task stays disabled, so nothing here can state more than the rows and the posting's own fields say.

    "Meets 6 of 8 required items. No evidence for Kubernetes. Remote, India."

No I/O, nothing logged.
"""

from collections.abc import Sequence
from functools import cache

import geonamescache  # type: ignore[import-untyped]

from .checker import JobFacts
from .validator import EvidenceRow

MAX_NAMED = 80  # characters of a requirement's words quoted in the line (assumption)
_ARRANGEMENT_WORDS = {
    "onsite": "on-site",
    "hybrid": "hybrid",
    "remote": "remote",
}  # in this order, whatever the posting's


@cache
def _country_names() -> dict[str, str]:
    return {iso: row["name"] for iso, row in geonamescache.GeonamesCache().get_countries().items()}


def _short(words: str) -> str:
    """A requirement's words on one line, without closing punctuation, cut at MAX_NAMED characters with an ellipsis."""
    flat = " ".join(words.split()).rstrip(".;:, ")
    return flat if len(flat) <= MAX_NAMED else flat[: MAX_NAMED - 1].rstrip() + "…"


def _arrangements(job: JobFacts) -> str:
    named = [word for kind, word in _ARRANGEMENT_WORDS.items() if kind in job.work_arrangements]
    if not named:
        return ""
    phrase = named[0] if len(named) == 1 else f"{', '.join(named[:-1])} or {named[-1]}"
    return phrase[0].upper() + phrase[1:]


def _where(job: JobFacts) -> str:
    country = job.country and _country_names().get(job.country, job.country)  # an unknown code is shown as it came
    place = ", ".join(part for part in (job.city, country) if part)
    mode = _arrangements(job)
    if mode and place:
        return f"{mode}, {place}."
    if place:
        return f"{place}."
    return f"{mode}. Location not stated." if mode else "Location not stated."


def render(rows: Sequence[EvidenceRow], job: JobFacts) -> str:
    """How many required items are met, the first required item that is not met and the first with no evidence (if any),
    then where the job is. Preferred items are not named: they are in the table."""
    required = [row for row in rows if row.importance == "required"]
    if required:
        met = sum(row.verdict == "met" for row in required)
        sentences = [f"Meets {met} of {len(required)} required item{'s' if len(required) != 1 else ''}."]
    else:
        sentences = ["No required items listed."]
    for verdict, template in (("not_met", "Not met: {}."), ("no_evidence", "No evidence for {}.")):
        first = next((row for row in required if row.verdict == verdict), None)
        if first is not None:
            sentences.append(template.format(_short(first.text)))
    sentences.append(_where(job))
    return " ".join(sentences)
