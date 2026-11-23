"""Model spend of a campaign and of a person (GRD-07; DATA_MODEL 6.6).

Spend is the sum of `llm_calls.cost_microusd`, which the gateway's attribution writes for every call made for a
campaign. A calendar month is read in UTC (assumption: one rule for every person; a person's own time zone would
move the
reset by a few hours). Budgets are compared in micro-dollars, never floats.
"""

from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import config

from .policy import SubjectGate


def current_month() -> date:
    """The first day of this calendar month in UTC."""
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def spent_microusd(
    conn: Connection, *, campaign_id: UUID | None = None, user_id: UUID | None = None, month: date | None = None
) -> int:
    """Micro-dollars spent on the calls that match every filter given: one campaign, one person, one calendar month
    (UTC,
    the month that starts on `month`'s first day). With no filter it is every call there is."""
    first = None if month is None else date(month.year, month.month, 1)
    spent = conn.execute(
        text(
            """SELECT COALESCE(sum(l.cost_microusd), 0) FROM llm_calls l
               WHERE (CAST(:campaign AS uuid) IS NULL OR l.campaign_id = :campaign)
                 AND (CAST(:user AS uuid) IS NULL OR l.user_id = :user)
                 AND (CAST(:first AS date) IS NULL
                      OR (l.created_at AT TIME ZONE 'UTC' >= CAST(:first AS date)
                          AND l.created_at AT TIME ZONE 'UTC' < CAST(:first AS date) + interval '1 month'))"""
        ),
        {"campaign": campaign_id, "user": user_id, "first": first},
    ).scalar_one()
    return int(spent)


def limits_reached(conn: Connection, gate: SubjectGate, estimate_microusd: int = 0) -> tuple[bool, bool]:
    """(the campaign's budget is used up, the person's monthly budget is used up) once `estimate_microusd` more is
    spent:
    a limit counts as used up when spend has reached it or the estimate would pass it."""
    settings = config.get()
    campaign = spent_microusd(conn, campaign_id=gate.id)
    month = spent_microusd(conn, user_id=gate.user_id, month=current_month())
    return (
        campaign >= gate.budget_microusd or campaign + estimate_microusd > gate.budget_microusd,
        month >= settings.monthly_budget_microusd or month + estimate_microusd > settings.monthly_budget_microusd,
    )


def budget_ok(conn: Connection, gate: SubjectGate, estimate_microusd: int) -> bool:
    """True when `estimate_microusd` more fits both the campaign's budget and the person's month."""
    return not any(limits_reached(conn, gate, estimate_microusd))
