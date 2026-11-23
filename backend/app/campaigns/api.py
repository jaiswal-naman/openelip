"""The only campaigns file other modules may import (chat and the brief, ARCHITECTURE layer 7): read views of a person's
campaigns. Nothing here writes, starts, approves or sends, and every function takes the caller's connection and the
person's id and filters by it.

  board, detail                      the campaign board and one campaign (GET /v1/campaigns and /v1/campaigns/{id})
  for_job, open_campaigns            a job's campaign (open, else the latest) and the open ones, as chat reads them
  dossier_for_job, blocks_for_job    the cited dossier of a job's campaign, and why work stopped on it
  open_campaign_for_job              the id of the job's open campaign, or None
  brief_items, approval_lines, action_lines, undoable
                                     what the morning brief lists: pending approvals and recent agent activity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from . import views
from .views import BriefLine

__all__ = [
    "BriefCampaignItems",
    "BriefLine",
    "action_lines",
    "approval_lines",
    "blocks_for_job",
    "board",
    "brief_items",
    "detail",
    "dossier_for_job",
    "for_job",
    "open_campaign_for_job",
    "open_campaigns",
    "undoable",
]


@dataclass(frozen=True)
class BriefCampaignItems:
    """What a brief written now lists: pending approvals (soonest to expire first) and the actions executed, blocked or
    failed since the previous brief, as ids with their lines, and the follow-ups that can still be undone."""

    approvals: dict[UUID, BriefLine]  # in the order to list them
    activity: dict[int, BriefLine]
    undoable: list[dict[str, Any]]


def board(conn: Connection, user_id: UUID, *, include_closed: bool = False) -> dict[str, Any]:
    return views.board(conn, user_id, include_closed=include_closed)


def detail(conn: Connection, user_id: UUID, campaign_id: UUID) -> dict[str, Any] | None:
    return views.detail(conn, user_id, campaign_id)


def for_job(conn: Connection, user_id: UUID, job_id: int) -> dict[str, Any] | None:
    return views.for_job(conn, user_id, job_id)


def open_campaigns(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    return views.open_campaigns(conn, user_id)


def dossier_for_job(conn: Connection, user_id: UUID, job_id: int) -> dict[str, Any] | None:
    return views.dossier_for_job(conn, user_id, job_id)


def blocks_for_job(conn: Connection, user_id: UUID, job_id: int) -> list[dict[str, str]]:
    return views.blocks_for_job(conn, user_id, job_id)


def open_campaign_for_job(conn: Connection, user_id: UUID, job_id: int) -> UUID | None:
    return views.open_campaign_for_job(conn, user_id, job_id)


def approval_lines(conn: Connection, user_id: UUID, approval_ids: list[UUID]) -> dict[UUID, BriefLine]:
    return views.approval_lines(conn, user_id, approval_ids)


def action_lines(conn: Connection, user_id: UUID, action_ids: list[int]) -> dict[int, BriefLine]:
    return views.action_lines(conn, user_id, action_ids)


def undoable(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    return views.undoable(conn, user_id)


def brief_items(conn: Connection, user_id: UUID, since: datetime | None) -> BriefCampaignItems:
    """The brief's campaign items for this person: at most five pending approvals and five actions since `since` (None:
    no activity, there is no previous brief to measure from)."""
    approval_ids = views.pending_approval_ids(conn, user_id)
    action_ids = views.activity_action_ids(conn, user_id, since)
    lines = views.approval_lines(conn, user_id, approval_ids)
    acts = views.action_lines(conn, user_id, action_ids)
    return BriefCampaignItems(
        approvals={i: lines[i] for i in approval_ids if i in lines},
        activity={i: acts[i] for i in action_ids if i in acts},
        undoable=views.undoable(conn, user_id),
    )
