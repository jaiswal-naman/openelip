"""Mission control's read side for campaigns: the runs list and the cost per campaign (API 2.19, GRD-05, GRD-06).

Both routes run on the API login under the caller's id, so row-level security returns only their own rows; nothing here
opens a worker or match connection. The handlers are plain `def` (sync database work stays off the event loop). Nothing
here computes a figure: a cost is a sum of the ledger's rows, and a run's cost is the sum of its own model calls.
"""

import base64
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.match import api as match
from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.control_routes import LABEL, CampaignCost, MonthQuery, costs_by_campaign, parse_month
from app.platform.errors import ApiError
from app.platform.llm.gateway import usd
from app.platform.sessions import Principal, current_user

router = APIRouter(prefix="/v1/control")
Signed = Annotated[Principal, Depends(current_user)]

# Every agent role but `guardian` (a campaign's state-change run is not a run of an agent, decision 36); `scout` is the
# matching module's nightly and on-demand runs.
RunRole = Literal["scout", "researcher", "analyst", "writer", "operator", "chaser", "coach", "twin"]
RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
NOT_TIED = "Not tied to a campaign"


class RunCampaign(BaseModel):
    id: UUID
    title: str
    company_name: str


class Run(BaseModel):
    id: UUID
    source: Literal["campaign", "matching"]
    campaign: RunCampaign | None
    agent_role: str
    status: RunStatus
    blocked: int
    cost_usd: str | None  # null for matching runs: they carry no cost attribution, so it is unknown, never zero
    trigger: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None  # null while running


class Runs(BaseModel):
    runs: list[Run]
    server_time: datetime
    next_cursor: str | None


# ---- the cursor ----------------------------------------


def encode_cursor(run: Run) -> str:
    """Opaque text for the position after `run`: its start time, id and source."""
    raw = json.dumps([run.started_at.isoformat(), str(run.id), run.source])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        at, ident, source = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if source not in ("campaign", "matching"):
            raise ValueError(source)
        when = datetime.fromisoformat(at)
        if when.tzinfo is None:
            raise ValueError(at)
        return when, UUID(ident)
    except (ValueError, TypeError, UnicodeError):
        raise ApiError(
            422,
            "validation_failed",
            "One or more fields are invalid.",
            {"fields": [{"field": "cursor", "issue": "not a cursor of this list"}]},
        ) from None


# ---- GET /v1/control/runs ----------------------------------------


def _titles(conn: Connection, ids: set[UUID]) -> dict[UUID, RunCampaign]:
    """Title and company of the listed campaigns, read as the board card does (campaign joined to its job)."""
    if not ids:
        return {}
    rows = conn.execute(
        text(
            """SELECT c.id, j.title, j.company_name FROM campaigns c JOIN jobs j ON j.id = c.job_id
               WHERE c.id = ANY(CAST(:ids AS uuid[]))"""
        ),
        {"ids": list(ids)},
    )
    return {r.id: RunCampaign(id=r.id, title=r.title, company_name=r.company_name) for r in rows}


def _duration(started: datetime, finished: datetime | None) -> int | None:
    return None if finished is None else max(0, round((finished - started).total_seconds() * 1000))


@router.get("/runs", response_model=Envelope[Runs])
def read_runs(
    principal: Signed,
    campaign_id: UUID | None = None,
    status: RunStatus | None = None,
    agent_role: RunRole | None = None,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    before = None if cursor is None else decode_cursor(cursor)
    want = limit + 1
    with db.transaction(user_id=principal.user_id) as conn:
        agent_rows = []
        if agent_role != "scout":
            agent_rows = guardian.list_runs(
                conn,
                principal.user_id,
                campaign_id=campaign_id,
                status=status,
                agent_role=agent_role,
                before=before,
                limit=want,
            )
        match_rows = []
        if campaign_id is None and agent_role in (None, "scout"):  # matching runs belong to no campaign
            match_rows = match.list_runs(conn, principal.user_id, status=status, before=before, limit=want)
        titles = _titles(conn, {r.campaign_id for r in agent_rows if r.campaign_id})
        server_time = conn.execute(text("SELECT now()")).scalar_one()
    runs = [
        Run(
            id=r.id,
            source="campaign",
            campaign=titles.get(r.campaign_id) if r.campaign_id else None,
            agent_role=r.agent_role,
            status=r.status,  # type: ignore[arg-type]
            blocked=r.blocked,
            cost_usd=usd(r.cost_microusd),
            trigger=None,
            started_at=r.started_at,
            finished_at=r.finished_at,
            duration_ms=_duration(r.started_at, r.finished_at),
        )
        for r in agent_rows
    ] + [
        Run(
            id=m.id,
            source="matching",
            campaign=None,
            agent_role="scout",
            status=m.status,  # type: ignore[arg-type]
            blocked=0,
            cost_usd=None,
            trigger=m.trigger,
            started_at=m.started_at,
            finished_at=m.finished_at,
            duration_ms=_duration(m.started_at, m.finished_at),
        )
        for m in match_rows
    ]
    runs.sort(key=lambda r: (r.started_at, str(r.id)), reverse=True)
    page = runs[:limit]
    more = len(runs) > limit
    return envelope(Runs(runs=page, server_time=server_time, next_cursor=encode_cursor(page[-1]) if more else None))


# ---- GET /v1/control/costs/campaigns ----------------------------------------


class CampaignSpend(BaseModel):
    campaign_id: UUID | None  # null: the "Not tied to a campaign" group
    title: str
    company_name: str | None
    stage: str | None
    spent_usd: str  # the month's spend when a month is asked for, else the campaign's whole life
    lifetime_spent_usd: str
    budget_usd: str | None
    model_calls: int  # of the period


class CampaignCosts(BaseModel):
    month: str | None
    currency: Literal["USD"]
    total_usd: str
    model_calls: int
    groups: list[CampaignSpend]
    label: str  # "Measured: sums of recorded model calls."


def _sums(rows: list[CampaignCost]) -> dict[UUID | None, CampaignCost]:
    return {r.campaign_id: r for r in rows}


@router.get("/costs/campaigns", response_model=Envelope[CampaignCosts])
def campaign_costs(principal: Signed, month: MonthQuery = None) -> dict[str, Any]:
    first = parse_month(month)
    with db.transaction(user_id=principal.user_id) as conn:
        period = _sums(costs_by_campaign(conn, month=first))
        lifetime = _sums(costs_by_campaign(conn, month=None)) if first else period
        campaigns = conn.execute(
            text(
                """SELECT c.id, c.stage::text AS stage, c.budget_microusd, j.title, j.company_name
                   FROM campaigns c JOIN jobs j ON j.id = c.job_id
                   WHERE c.stage <> 'closed' OR c.id = ANY(CAST(:spent AS uuid[]))""",
            ),
            {"spent": [k for k in period if k is not None]},
        ).all()
    groups: list[tuple[int, CampaignSpend]] = []
    for c in campaigns:
        spent, life = period.get(c.id), lifetime.get(c.id)
        groups.append(
            (
                spent.microusd if spent else 0,
                CampaignSpend(
                    campaign_id=c.id,
                    title=c.title,
                    company_name=c.company_name,
                    stage=c.stage,
                    spent_usd=usd(spent.microusd if spent else 0),
                    lifetime_spent_usd=usd(life.microusd if life else 0),
                    budget_usd=usd(c.budget_microusd),
                    model_calls=spent.calls if spent else 0,
                ),
            )
        )
    groups.sort(key=lambda g: (-g[0], g[1].title, str(g[1].campaign_id)))
    loose, loose_life = period.get(None), lifetime.get(None)
    groups.append(
        (
            loose.microusd if loose else 0,
            CampaignSpend(
                campaign_id=None,
                title=NOT_TIED,
                company_name=None,
                stage=None,
                spent_usd=usd(loose.microusd if loose else 0),
                lifetime_spent_usd=usd(loose_life.microusd if loose_life else 0),
                budget_usd=None,
                model_calls=loose.calls if loose else 0,
            ),
        )
    )
    data = CampaignCosts(
        month=month,
        currency="USD",
        total_usd=usd(sum(micro for micro, _ in groups)),  # the total is the sum of the groups shown
        model_calls=sum(g.model_calls for _, g in groups),
        groups=[g for _, g in groups],
        label=LABEL,
    )
    return envelope(data)
