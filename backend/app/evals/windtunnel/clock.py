"""Virtual time for one attempt (INDEX decision 13).

Days pass in minutes: instead of waiting, the driver moves what the campaign is waiting for into the past, by the same
statements the Phase 7 `age` helpers use: `campaigns.last_update_at`, the due time of the campaign's waiting steps and
the run time of its queued `campaign.wake` jobs, and the finish time of its finished steps (the Chaser measures follow-up 2
from the end of follow-up 1, so a silent employer needs that moved too). It never touches an undo window (`agent_actions.undo_until` is
immutable and a real minute at least), an approval, or an introduction: no scenario depends on those clocks.

Because each attempt shifts only its own campaign, fifty attempts interleave in one process without disturbing each
other. VirtualClock converts a real moment of one attempt into its virtual day (days since the attempt began, counting
every shift made before that moment).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Connection, text

DAY = timedelta(days=1)


@dataclass
class VirtualClock:
    """One attempt's virtual time: real time since `started` plus every shift made so far."""

    started: datetime
    shifts: list[tuple[datetime, timedelta]] = field(default_factory=list)

    @property
    def offset(self) -> timedelta:
        return sum((delta for _, delta in self.shifts), timedelta())

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, delta: timedelta) -> None:
        self.shifts.append((datetime.now(UTC), delta))

    def day(self, at: datetime) -> float:
        """The virtual day of a real moment: days since the attempt began, with the shifts made up to then."""
        passed = sum((delta for when, delta in self.shifts if when <= at), timedelta())
        return (at - self.started + passed) / DAY

    def day_now(self) -> float:
        return self.day(datetime.now(UTC))


def next_due(conn: Connection, campaign_id: UUID, *, after: datetime | None = None) -> datetime | None:
    """The earliest time the campaign is waiting for: a waiting step's due time or a queued wake job's run time (only
    times later than `after` when it is given: a step still waiting for a person has a due time long past)."""
    due: datetime | None = conn.execute(
        text(
            """SELECT min(t) FROM (
                   SELECT due_at AS t FROM campaign_steps WHERE campaign_id = :c AND status = 'waiting'
                   UNION ALL
                   SELECT run_at FROM queue_jobs
                    WHERE kind = 'campaign.wake' AND status = 'queued' AND payload ->> 'campaign_id' = :cs
               ) x WHERE CAST(:after AS timestamptz) IS NULL OR t > CAST(:after AS timestamptz)"""
        ),
        {"c": campaign_id, "cs": str(campaign_id), "after": after},
    ).scalar_one()
    return due


def shift(conn: Connection, campaign_id: UUID, delta: timedelta) -> None:
    """Move this campaign's waiting back by `delta`: last news, waiting steps' due times, queued wake jobs."""
    arguments = {"c": campaign_id, "cs": str(campaign_id), "d": delta}
    conn.execute(text("UPDATE campaigns SET last_update_at = last_update_at - :d WHERE id = :c"), arguments)
    conn.execute(
        text("UPDATE campaign_steps SET due_at = due_at - :d WHERE campaign_id = :c AND status = 'waiting'"), arguments
    )
    conn.execute(
        text("UPDATE campaign_steps SET finished_at = finished_at - :d WHERE campaign_id = :c AND finished_at IS NOT NULL"),
        arguments,
    )
    conn.execute(
        text(
            """UPDATE queue_jobs SET run_at = run_at - :d
                WHERE kind = 'campaign.wake' AND status = 'queued' AND payload ->> 'campaign_id' = :cs"""
        ),
        arguments,
    )
