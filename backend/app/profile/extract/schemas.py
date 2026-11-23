"""The shapes in this package: what a model may return, and what the code hands on (AI_SYSTEM 4.2).

Every model schema forbids extra fields. A model that adds a statement, a quote or a duration is giving invalid output,
which the gateway asks for once more and then refuses. The model points at lines and copies names and dates as they are
written there; code does the rest (validate.py).
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleItem(_Strict):
    kind: Literal["role", "achievement", "project"]
    line_start: int
    line_end: int
    parent: int | None  # position in this answer's items of the role an achievement or project belongs to
    employer: str | None
    title: str | None
    start_date: str | None
    end_date: str | None
    is_current: bool | None


class RoleItems(_Strict):
    items: list[RoleItem]


class EducationSkillItem(_Strict):
    kind: Literal["education", "skill", "certification"]
    line_start: int
    line_end: int
    institution: str | None
    degree: str | None
    name: str | None  # a skill or a certification
    start_date: str | None
    end_date: str | None


class EducationSkillItems(_Strict):
    items: list[EducationSkillItem]


class StatementItem(_Strict):
    line_start: int
    line_end: int


class StatementItems(_Strict):
    items: list[StatementItem]


@dataclass(frozen=True)
class CandidateFact:
    """One fact the resume supports. statement and source_quote are the resume's own text at source_span, copied by
    code; attributes hold only values found in that text. parent_index is a position in the kept list: the role an
    achievement or project belongs to."""

    kind: str  # a fact_kind (DATA_MODEL 2.1)
    statement: str
    attributes: dict[str, Any]
    parent_index: int | None
    source_span: tuple[int, int]  # half-open character range into the cleaned resume text
    source_quote: str


@dataclass(frozen=True)
class UnverifiedItem:
    """Something the model pointed at that the code could not confirm. reason is a short code (validate.py lists them).
    source_span and source_quote are the cited lines' own text, or None when those lines do not exist."""

    kind: str
    reason: str
    source_span: tuple[int, int] | None
    source_quote: str | None
