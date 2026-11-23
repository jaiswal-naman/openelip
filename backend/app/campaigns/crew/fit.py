"""The Analyst (AI_SYSTEM 6.5): the job's requirements grouped by the verdicts the matching step already stored.

No model call. A requirement with no stored verdict is listed as not assessed, never guessed. The result is the
output of
the campaign's `fit` step: ids and counts only, no text a model wrote.
"""

from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from app.tailor import api as tailor

VERDICTS = ("met", "partly_met", "not_met", "no_evidence")
MAX_TALKING_POINTS = 3  # assumption: a short list the candidate can hold in mind


class VerdictRow(Protocol):
    """What analyse reads of a stored verdict (match.api.DecisionEvidence has these attributes)."""

    @property
    def requirement_id(self) -> int: ...
    @property
    def verdict(self) -> str: ...
    @property
    def fact_ids(self) -> Sequence[Any]: ...


def analyse(verdict_rows: Iterable[VerdictRow], requirements: Sequence[tailor.Requirement]) -> dict[str, Any]:
    """{met, partly_met, not_met, no_evidence, not_assessed, talking_points}: each a list in requirement order of
    {"requirement_id", "fact_ids"}. talking_points are the first three `met` requirements that are required (else
    preferred ones fill the gap), in requirement order."""
    by_requirement = {row.requirement_id: row for row in verdict_rows}
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in (*VERDICTS, "not_assessed")}
    importance = {}
    for requirement in requirements:
        importance[requirement.id] = requirement.importance
        row = by_requirement.get(requirement.id)
        if row is None or row.verdict not in VERDICTS:
            groups["not_assessed"].append({"requirement_id": requirement.id, "fact_ids": []})
        else:
            groups[row.verdict].append({"requirement_id": requirement.id, "fact_ids": [str(f) for f in row.fact_ids]})
    met = groups["met"]
    ordered = [m for m in met if importance[m["requirement_id"]] == "required"]
    ordered += [m for m in met if importance[m["requirement_id"]] != "required"]
    groups["talking_points"] = ordered[:MAX_TALKING_POINTS]
    return groups
