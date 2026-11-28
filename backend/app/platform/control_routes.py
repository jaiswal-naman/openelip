"""The signed-in person's own cost view (API.md 2.19 and 3.17).

The models are the contract: the web types are generated from them. The ledger is read under the caller's user id, so
row-level security returns only their own rows; no query here filters by person. model_registry has no row-level
security, but the join to it can only reach the models those rows point at, so a person never sees a model that only
someone else's calls used.
"""

from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from . import db
from .api import Envelope, envelope
from .llm.gateway import usd
from .sessions import Principal, current_user

router = APIRouter(prefix="/v1/control")

GroupBy = Literal["month", "task"]  # per-campaign costs: campaigns.control_routes (Phase 8 decision 25)

LABEL = "Measured: sums of recorded model calls."
# The model is the registry row a call points at, named as provider/model_name (the pair is the registry's key).
_MODEL = "m.provider || '/' || m.model_name"
_SUMS = (
    "count(*), coalesce(sum(l.input_tokens), 0), coalesce(sum(l.output_tokens), 0), coalesce(sum(l.cost_microusd), 0), "
    f"round(avg(l.latency_ms)), array_agg(DISTINCT {_MODEL} ORDER BY {_MODEL})"
)
_FROM = "FROM llm_calls l JOIN model_registry m ON m.id = l.model_id"
# Months are UTC months. ponytail: per-person time zones would read users.timezone.
_IN_MONTH = "WHERE l.created_at >= :start AND l.created_at < :end"
_QUERIES = {
    "month": f"SELECT to_char(l.created_at AT TIME ZONE 'UTC', 'YYYY-MM'), NULL::text, {_SUMS} {_FROM} {{where}} "
    "GROUP BY 1 ORDER BY 1 DESC",
    "task": f"SELECT NULL::text, l.task, {_SUMS} {_FROM} {{where}} "
    "GROUP BY l.task ORDER BY sum(l.cost_microusd) DESC, l.task",
}
# "2026-11": the month is 01 to 12, so a person cannot ask for month 13 (422 validation_failed)
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
MonthQuery = Annotated[str | None, Query(pattern=MONTH_PATTERN)]


def parse_month(month: str | None) -> date | None:
    """The first day of a `YYYY-MM` month (already checked by MONTH_PATTERN), or None."""
    return None if month is None else date(int(month[:4]), int(month[5:]), 1)


def _bounds(month: date) -> dict[str, datetime]:
    """The UTC month as `[start, end)`."""
    end = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return {
        "start": datetime(month.year, month.month, 1, tzinfo=timezone.utc),
        "end": datetime(end.year, end.month, 1, tzinfo=timezone.utc),
    }


class CampaignCost(NamedTuple):
    """What the caller's model calls of one campaign cost (None: calls tied to no campaign)."""

    campaign_id: UUID | None
    microusd: int
    calls: int


def costs_by_campaign(conn: Connection, *, month: date | None = None) -> list[CampaignCost]:
    """One row per campaign the caller's ledger rows name, and one for those with no campaign, in the UTC `month` or
    over all time. Row-level security limits the ledger to the caller's own rows."""
    where = " WHERE l.created_at >= :start AND l.created_at < :end" if month else ""
    rows = conn.execute(
        text(
            "SELECT l.campaign_id, coalesce(sum(l.cost_microusd), 0), count(*) FROM llm_calls l"
            f"{where} GROUP BY l.campaign_id"
        ),
        _bounds(month) if month else {},
    ).all()
    return [CampaignCost(r[0], int(r[1]), int(r[2])) for r in rows]


class CostRow(BaseModel):
    period: str | None  # "2026-11" when grouped by month, else null
    task: str | None  # "ping" when grouped by task, else null
    calls: int
    cost_usd: str  # a decimal string with up to six places (API.md 1.4)
    input_tokens: int
    output_tokens: int
    avg_latency_ms: int  # the mean measured duration of these calls, to the nearest millisecond; failed attempts count
    models: list[str]  # "provider/model_name" of every model these calls used, sorted, each once


class Costs(BaseModel):
    group_by: GroupBy
    currency: Literal["USD"]
    total_usd: str
    model_calls: int
    rows: list[CostRow]
    label: str  # "Measured: sums of recorded model calls."


@router.get("/costs", response_model=Envelope[Costs])
def costs(
    group_by: GroupBy = "month", month: MonthQuery = None, principal: Principal = Depends(current_user)
) -> dict[str, Any]:
    first = parse_month(month)
    query = _QUERIES[group_by].format(where=_IN_MONTH if first else "")
    with db.transaction(user_id=principal.user_id) as conn:
        found = conn.execute(text(query), _bounds(first) if first else {}).all()
    rows = [
        CostRow(
            period=p,
            task=t,
            calls=n,
            cost_usd=usd(int(c)),
            input_tokens=int(i),
            output_tokens=int(o),
            avg_latency_ms=int(ms),
            models=models,
        )
        for p, t, n, i, o, c, ms, models in found
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
