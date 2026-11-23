"""The only apply file other modules may import: what the application tracker and the packets offer the rest of the
application.

  ApplicationView, list_applications   the tracker (APP-01)
  request_packet, draft_packet         ask for a packet and run its drafting (the injection suite calls these)
  get_packet, current_for_job          read a packet by id, or the current one of a job
  PacketView                           what a packet read returns
  link_campaign, application_for_campaign, open_application_for_job, mark_packet_approved
                                       what campaigns (Phase 7) need: link an application to a campaign, read the one a
                                       campaign has, the one a person has for a job, and mark a packet approved

request_packet and draft_packet pass keyword arguments to apply.packet unchanged, so what that module accepts later
(a campaign id, dossier claims) reaches callers without a change here.

Nothing else in app.apply is part of that interface.
"""

from uuid import UUID

from sqlalchemy import Connection, text

from .packet import PacketView, current_for_job
from .packet import draft as draft_packet
from .packet import get as get_packet
from .packet import request as request_packet
from .tracker import ApplicationView, list_applications, view_one

__all__ = [
    "ApplicationView",
    "PacketView",
    "application_for_campaign",
    "current_for_job",
    "draft_packet",
    "get_packet",
    "link_campaign",
    "list_applications",
    "mark_packet_approved",
    "open_application_for_job",
    "request_packet",
]


def link_campaign(conn: Connection, application_id: UUID, campaign_id: UUID) -> bool:
    """Tie an application to a campaign of the same person, so the campaign can read the status the person records.
    False when there is no such pair (another person's campaign or application, or an unknown id): nothing changes."""
    return (
        conn.execute(
            text(
                """UPDATE applications a SET campaign_id = c.id
                   FROM campaigns c WHERE a.id = :application AND c.id = :campaign AND c.user_id = a.user_id"""
            ),
            {"application": application_id, "campaign": campaign_id},
        ).rowcount
        > 0
    )


def application_for_campaign(conn: Connection, campaign_id: UUID) -> ApplicationView | None:
    """The application linked to the campaign, with its history, or None while there is none."""
    row = conn.execute(
        text("SELECT id, user_id FROM applications WHERE campaign_id = :c ORDER BY created_at, id LIMIT 1"),
        {"c": campaign_id},
    ).one_or_none()
    return None if row is None else view_one(conn, row.user_id, row.id)


def open_application_for_job(conn: Connection, user_id: UUID, job_id: int) -> ApplicationView | None:
    """The person's tracked application for the job (one per job), or None when they have not recorded one."""
    row = conn.execute(
        text("SELECT id FROM applications WHERE user_id = :u AND job_id = :j"), {"u": user_id, "j": job_id}
    ).one_or_none()
    return None if row is None else view_one(conn, user_id, row.id)


def mark_packet_approved(conn: Connection, packet_id: UUID) -> bool:
    """Move a ready packet to approved, once, with the time. True when this call did it, False when the packet is gone,
    superseded, still drafting or already approved (so a repeated approval changes nothing)."""
    return (
        conn.execute(
            text(
                """UPDATE application_packets SET status = 'approved', approved_at = now()
                   WHERE id = :p AND status = 'ready'"""
            ),
            {"p": packet_id},
        ).rowcount
        > 0
    )
