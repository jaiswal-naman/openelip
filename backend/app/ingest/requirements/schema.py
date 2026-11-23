"""The model's answer for job.requirements.extract: AI_SYSTEM 4.3's JSON schema as Pydantic models.

Only shapes live here. Adjacency, quotes, years and the 30-item cap are the validator's job (AI_SYSTEM 2.3): a numeric
or length bound in the schema is refused by the provider's constrained decoding.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Importance = Literal["required", "preferred"]  # DATA_MODEL requirement_importance
Kind = Literal["skill", "experience", "education", "certification", "authorization", "location", "other"]


class RequirementItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[int]  # the numbers of the segments that state the requirement
    quote: str | None  # exact words inside those segments, or None when all of their text is the requirement
    importance: Importance
    kind: Kind
    min_years: int | None


class RequirementList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[RequirementItem]
