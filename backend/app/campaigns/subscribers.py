"""The campaign event subscriptions (ARCHITECTURE 3.3, INDEX "Shared contracts"). Importing this module in both
processes fills the start-up registry; the handlers run only in the worker, as queue jobs.

    introduction.accepted      -> campaign.from_introduction         start a campaign at `applied`
    introduction.answered      -> campaign.introduction_answered     wake the linked campaign (yes or no)
    application.status_changed -> campaign.application_changed       link the application, wake the campaign
    guardian.action_changed    -> campaign.resume                    straight to the campaign's thread

Hiring, apply and the Guardian only publish ids; each handler here reads the rows again as the person the event names
and refuses a row of anyone else (the worker's login sees every row, so the check is in the query). Each wake is one
`campaign.resume` job with the INDEX cause, so a delivery that runs twice wakes the campaign once.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.apply import api as apply
from app.hiring import api as hiring
from app.platform import db, events, queue

from . import service, stages

events.subscribe("introduction.accepted", "campaign.from_introduction")
events.subscribe("introduction.answered", "campaign.introduction_answered")
events.subscribe("application.status_changed", "campaign.application_changed")
events.subscribe("guardian.action_changed", "campaign.resume")  # the event's own payload and key are the resume job's


@queue.handler("campaign.from_introduction")
def from_introduction(conn: Connection, payload: dict[str, Any]) -> None:
    """Accepting an introduction is applying: start a campaign at `applied` for the role's job."""
    user_id = UUID(payload["user_id"])
    with db.transaction(user_id, role="worker") as worker:
        service.from_introduction(worker, user_id, UUID(payload["introduction_id"]))


def _touch(conn: Connection, campaign_id: UUID) -> None:
    """News from outside: the Chaser's quiet days count from here (INDEX decision for `last_update_at`)."""
    conn.execute(text("UPDATE campaigns SET last_update_at = now() WHERE id = :c"), {"c": campaign_id})


@queue.handler("campaign.introduction_answered")
def introduction_answered(conn: Connection, payload: dict[str, Any]) -> None:
    """The employer answered: wake the campaign the introduction started, with cause `intro{id}.{answer}`."""
    user_id = UUID(payload["user_id"])
    introduction_id = UUID(payload["introduction_id"])
    with db.transaction(user_id, role="worker") as worker:
        ref = hiring.introduction_ref(worker, introduction_id)
        if ref is None or ref.user_id != user_id or ref.employer_answer is None:
            return
        campaign_id = worker.execute(
            text("SELECT id FROM campaigns WHERE user_id = :u AND introduction_id = :i AND stage <> 'closed'"),
            {"u": user_id, "i": introduction_id},
        ).scalar_one_or_none()
        if campaign_id is None:
            return
        _touch(worker, campaign_id)
        stages.enqueue_wake(worker, campaign_id, user_id, f"intro{introduction_id}.{ref.employer_answer}")


@queue.handler("campaign.application_changed")
def application_changed(conn: Connection, payload: dict[str, Any]) -> None:
    """A status was recorded: tie the application to the job's open campaign when it has none yet, and wake it."""
    user_id = UUID(payload["user_id"])
    event_id = int(payload["application_event_id"])
    with db.transaction(user_id, role="worker") as worker:
        application = worker.execute(
            text(
                """SELECT a.id, a.job_id, a.campaign_id FROM application_events e
                   JOIN applications a ON a.id = e.application_id AND a.user_id = e.user_id
                   WHERE e.id = :e AND e.user_id = :u"""
            ),
            {"e": event_id, "u": user_id},
        ).one_or_none()
        if application is None:
            return
        campaign_id = (
            application.campaign_id
            or worker.execute(
                text("SELECT id FROM campaigns WHERE user_id = :u AND job_id = :j AND stage <> 'closed'"),
                {"u": user_id, "j": application.job_id},
            ).scalar_one_or_none()
        )
        if campaign_id is None:
            return
        if application.campaign_id is None:
            apply.link_campaign(worker, application.id, campaign_id)
        _touch(worker, campaign_id)
        stages.enqueue_wake(worker, campaign_id, user_id, f"app{event_id}")
