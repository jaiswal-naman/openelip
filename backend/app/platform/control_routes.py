"""The signed-in person's own cost view (plan 01-04 implements the body; API.md 2.19 and 3.17).

The models are the contract: the web types are generated from them.
"""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from .api import Envelope
from .errors import ApiError

router = APIRouter(prefix="/v1/control")

GroupBy = Literal["month", "task"]  # "campaign" is added in Phase 7


class CostRow(BaseModel):
    period: str | None  # "2026-11" when grouped by month, else null
    task: str | None  # "ping" when grouped by task, else null
    calls: int
    cost_usd: str  # a decimal string with up to six places (API.md 1.4)
    input_tokens: int
    output_tokens: int


class Costs(BaseModel):
    group_by: GroupBy
    currency: Literal["USD"]
    total_usd: str
    model_calls: int
    rows: list[CostRow]
    label: str  # "Measured: sums of recorded model calls."


@router.get("/costs", response_model=Envelope[Costs])
def costs(group_by: GroupBy = "month") -> dict[str, Any]:
    raise ApiError(501, "not_implemented", "Not implemented yet.")
