"""The signed-in person's own cost view (API.md 2.19 and 3.17).

The models are the contract: the web types are generated from them. The ledger is read under the caller's user id, so
row-level security returns only their own rows; no query here filters by person.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from . import db
from .api import Envelope, envelope
from .llm.gateway import usd
from .sessions import Principal, current_user

router = APIRouter(prefix="/v1/control")

GroupBy = Literal["month", "task"]  # "campaign" is added in Phase 7

LABEL = "Measured: sums of recorded model calls."
_SUMS = "count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), coalesce(sum(cost_microusd), 0)"
# Months are UTC months. ponytail: per-person time zones would read users.timezone.
_QUERIES = {
    "month": f"SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM'), NULL::text, {_SUMS} "
    "FROM llm_calls GROUP BY 1 ORDER BY 1 DESC",
    "task": f"SELECT NULL::text, task, {_SUMS} FROM llm_calls GROUP BY task ORDER BY sum(cost_microusd) DESC, task",
}


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
def costs(group_by: GroupBy = "month", principal: Principal = Depends(current_user)) -> dict[str, Any]:
    with db.transaction(user_id=principal.user_id) as conn:
        found = conn.execute(text(_QUERIES[group_by])).all()
    rows = [
        CostRow(period=p, task=t, calls=n, cost_usd=usd(int(c)), input_tokens=int(i), output_tokens=int(o))
        for p, t, n, i, o, c in found
    ]
    total = sum(int(row[5]) for row in found)
    data = Costs(
        group_by=group_by,
        currency="USD",
        total_usd=usd(total),
        model_calls=sum(r.calls for r in rows),
        rows=rows,
        label=LABEL,
    )
    return envelope(data)
