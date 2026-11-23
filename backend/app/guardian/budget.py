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
    conn: Connection,
    *,
    campaign_id: UUID | None = None,
    user_id: UUID | None = None,
    month: date | None = None,
    leave_out_twin: bool = False,
) -> int:
    """Micro-dollars spent on the calls that match every filter given: one campaign, one person, one calendar month
    (UTC,
    the month that starts on `month`'s first day). With no filter it is every call there is. `leave_out_twin` skips
    the calls of Twin runs, which the Twin session's own cap governs (INDEX decision 10)."""
    first = None if month is None else date(month.year, month.month, 1)
    spent = conn.execute(
        text(
            """SELECT COALESCE(sum(l.cost_microusd), 0) FROM llm_calls l
               WHERE (CAST(:campaign AS uuid) IS NULL OR l.campaign_id = :campaign)
                 AND (CAST(:user AS uuid) IS NULL OR l.user_id = :user)
                 AND (NOT :leave_out_twin
                      OR NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.id = l.agent_run_id AND r.agent_role = 'twin'))
                 AND (CAST(:first AS date) IS NULL
                      OR (l.created_at AT TIME ZONE 'UTC' >= CAST(:first AS date)
                          AND l.created_at AT TIME ZONE 'UTC' < CAST(:first AS date) + interval '1 month'))"""
        ),
        {"campaign": campaign_id, "user": user_id, "first": first, "leave_out_twin": leave_out_twin},
    ).scalar_one()
    return int(spent)


def limits_reached(conn: Connection, gate: SubjectGate, estimate_microusd: int = 0) -> tuple[bool, bool]:
    """(the campaign's budget is used up, the person's monthly budget is used up) once `estimate_microusd` more is
    spent:
    a limit counts as used up when spend has reached it or the estimate would pass it."""
    settings = config.get()
    campaign = spent_microusd(conn, campaign_id=gate.id)
    month = spent_microusd(conn, user_id=gate.user_id, month=current_month(), leave_out_twin=True)
    return (
        campaign >= gate.budget_microusd or campaign + estimate_microusd > gate.budget_microusd,
        month >= settings.monthly_budget_microusd or month + estimate_microusd > settings.monthly_budget_microusd,
    )


def budget_ok(conn: Connection, gate: SubjectGate, estimate_microusd: int) -> bool:
    """True when `estimate_microusd` more fits both the campaign's budget and the person's month."""
    return not any(limits_reached(conn, gate, estimate_microusd))


def session_spent_microusd(conn: Connection, session_id: UUID) -> int:
    """Micro-dollars of model calls made for one Twin session (through its question runs): the session cap's meter."""
    return int(
        conn.execute(
            text(
                """SELECT COALESCE(sum(l.cost_microusd), 0) FROM llm_calls l
                   JOIN agent_runs r ON r.id = l.agent_run_id WHERE r.twin_session_id = :s"""
            ),
            {"s": session_id},
        ).scalar_one()
    )
