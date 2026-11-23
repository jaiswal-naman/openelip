"""The rules ranker (AI_SYSTEM 4.5): fit from stored verdicts, the final score, who may be shown, and the day's five.

Code over verdicts, no model. Every weight and threshold below is an assumption of the design, to be tuned on the gold
sets; RANKER_VERSION names this set of rules and is stored on every match run. A learned ranker is v2 (OUT-01).
No I/O, nothing logged.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .checker import company_key
from .validator import EvidenceRow

RANKER_VERSION = "rules-2026-11-a"

# assumption (AI_SYSTEM 4.5): requirement weights and verdict scores; "not_met" costs a point on a required item only
REQUIRED_WEIGHT, PREFERRED_WEIGHT = 2, 1
VERDICT_SCORE = {"met": 1.0, "partly_met": 0.5, "no_evidence": 0.0, "not_met": 0.0}
NOT_MET_REQUIRED_SCORE = -1.0
# assumption (AI_SYSTEM 4.5): final = fit + 0.10 x soft-preference match - 0.05 x seniority distance + 0.02 x recency
PREFERENCE_WEIGHT = 0.10
SENIORITY_WEIGHT = 0.05
RECENCY_WEIGHT = 0.02
# assumption (AI_SYSTEM 4.5): shown only at 0.6 or more, with at least half of the required items met or partly met
SHOW_THRESHOLD = 0.6
REQUIRED_SHARE = 0.5
# assumption: a posting changed within a week counts fully, within a month half, older not at all
FRESH_DAYS, RECENT_DAYS = 7, 30

_NOT_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Scored:
    """A job that has verdicts: what pick() needs to decide whether it is shown today. `final` is final_score()."""

    job_id: int
    company_name: str
    title: str
    final: float
    rows: tuple[EvidenceRow, ...]
    weak_fit: bool = False  # the validator's rule 6


@dataclass(frozen=True)
class Picked:
    shown: tuple[Scored, ...]  # best first: rank_position is the index plus one
    held: dict[int, str]  # job_id -> match_reason, for every other job given


def _weight(row: EvidenceRow) -> int:
    return REQUIRED_WEIGHT if row.importance == "required" else PREFERRED_WEIGHT


def _verdict_score(row: EvidenceRow) -> float:
    if row.verdict == "not_met" and row.importance == "required":
        return NOT_MET_REQUIRED_SCORE
    return VERDICT_SCORE[row.verdict]


def fit_score(rows: Sequence[EvidenceRow]) -> float:
    """The weighted sum of verdict scores over the weighted count. A job with no verdicts has fit 0."""
    weight = sum(_weight(row) for row in rows)
    return sum(_weight(row) * _verdict_score(row) for row in rows) / weight if weight else 0.0


def final_score(fit: float, preference_match: float, seniority_distance: float, recency_bucket: float) -> float:
    """preference_match is in -1..1 (the share of soft preferences the job meets, minus the share it breaks);
    seniority_distance counts the seniority steps between the job and the candidate's target."""
    return (
        fit
        + PREFERENCE_WEIGHT * preference_match
        - SENIORITY_WEIGHT * seniority_distance
        + RECENCY_WEIGHT * recency_bucket
    )


def prerank_score(rrf: float, preference_match: float, seniority_distance: float, recency_bucket: float) -> float:
    """What decides which retrieved jobs are scored by the model: the same shape as final_score over the fusion score
    (normalized by the caller to 0..1 against the best of the retrieved set), since no verdicts exist yet."""
    return final_score(rrf, preference_match, seniority_distance, recency_bucket)


def age_bucket(age_days: float) -> float:
    """The recency bucket of a posting that last changed this many days ago."""
    if age_days <= FRESH_DAYS:
        return 1.0
    return 0.5 if age_days <= RECENT_DAYS else 0.0


def is_showable(final: float, rows: Sequence[EvidenceRow]) -> bool:
    required = [row for row in rows if row.importance == "required"]
    supported = sum(row.verdict in ("met", "partly_met") for row in required)
    return final >= SHOW_THRESHOLD and supported >= REQUIRED_SHARE * len(required)


def _group(job: Scored) -> tuple[str, str]:
    """Near-duplicates: the same company and the same title once case, punctuation and spacing are ignored."""
    return company_key(job.company_name), _NOT_ALNUM.sub(" ", job.title.lower()).strip()


def _why_held(job: Scored) -> str:
    # Weak support is named first: that reason is what the weekly human audit samples (AI_SYSTEM 4.4 rule 6).
    if not job.weak_fit and any(r.importance == "required" and r.verdict == "not_met" for r in job.rows):
        return "required_not_met"
    return "weak_fit"


def pick(scored: Iterable[Scored], open_slots: int) -> Picked:
    """Today's matches: the best showable job of each near-duplicate group, best final score first (a tie goes to the
    lower job id, so a run repeats), at most open_slots of them. Every other job gets its reason: required_not_met,
    weak_fit (below the threshold, or weak lexical support), or below_daily_cap (showable but no slot, or a
    near-duplicate of one that is shown)."""
    held: dict[int, str] = {}
    candidates: list[Scored] = []
    groups: set[tuple[str, str]] = set()
    for job in sorted(scored, key=lambda j: (-j.final, j.job_id)):
        if job.weak_fit or not is_showable(job.final, job.rows):
            held[job.job_id] = _why_held(job)
        elif _group(job) in groups:
            held[job.job_id] = "below_daily_cap"
        else:
            groups.add(_group(job))
            candidates.append(job)
    shown = tuple(candidates[: max(open_slots, 0)])
    held.update({job.job_id: "below_daily_cap" for job in candidates[len(shown) :]})
    return Picked(shown, held)
