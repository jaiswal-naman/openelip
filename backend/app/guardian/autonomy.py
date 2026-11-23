"""The autonomy dial (GRD-01, GRD-04; API 2.13 and 3.9).

One row per action type with the levels that type allows. No row means `ask_first`. The database refuses `tell_me_after`
for the two always-approved types as well; this module refuses it first, with a code the page can show. The version is a
hash of the three levels and the undo window (INDEX decision 21): there is no version column to compare.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import config
from app.platform.errors import ApiError

from .policy import ACTION_TYPES

ALLOWED_LEVELS: dict[str, tuple[str, ...]] = {
    "submit_application": ("ask_first",),
    "send_follow_up": ("ask_first", "never"),
    "twin_answer": ("ask_first", "tell_me_after", "never"),
}
DEFAULT_LEVEL = "ask_first"
UNDO_WINDOW_RANGE = (1, 60)  # minutes; the same bounds as users.undo_window_minutes
PROFILE_SHARING = "Only when you accept an introduction."  # fixed: sharing a profile is never a setting (7.1)


@dataclass(frozen=True)
class AutonomyRow:
    action_type: str
    level: str
    allowed: tuple[str, ...]


@dataclass(frozen=True)
class AutonomyView:
    rows: tuple[AutonomyRow, ...]
    undo_window_minutes: int
    profile_sharing: str
    default_budget_microusd: int
    version: str


def _version(levels: Mapping[str, str], window: int) -> str:
    raw = json.dumps({"levels": {key: levels[key] for key in sorted(levels)}, "undo": window}, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def level_of(conn: Connection, user_id: UUID, action_type: str) -> str:
    """The person's level for one type; `ask_first` when they never set one."""
    found = conn.execute(
        text("SELECT level::text FROM autonomy_settings WHERE user_id = :u AND action_type = CAST(:t AS action_type)"),
        {"u": user_id, "t": action_type},
    ).scalar_one_or_none()
    return found or DEFAULT_LEVEL


def view(conn: Connection, user_id: UUID) -> AutonomyView:
    """The three rows with their allowed levels, the undo window and the version."""
    stored = {
        row.action_type: row.level
        for row in conn.execute(
            text(
                "SELECT action_type::text AS action_type, level::text AS level "
                "FROM autonomy_settings WHERE user_id = :u"
            ),
            {"u": user_id},
        )
    }
    window = int(conn.execute(text("SELECT undo_window_minutes FROM users WHERE id = :u"), {"u": user_id}).scalar_one())
    levels = {kind: stored.get(kind, DEFAULT_LEVEL) for kind in ACTION_TYPES}
    return AutonomyView(
        rows=tuple(AutonomyRow(kind, levels[kind], ALLOWED_LEVELS[kind]) for kind in ACTION_TYPES),
        undo_window_minutes=window,
        profile_sharing=PROFILE_SHARING,
        default_budget_microusd=config.get().campaign_budget_microusd,
        version=_version(levels, window),
    )


def put(
    conn: Connection,
    user_id: UUID,
    *,
    levels: Mapping[str, str] | None = None,
    undo_window_minutes: int | None = None,
    version: str | None = None,
) -> AutonomyView:
    """Replace the given levels and the undo window. `version` is the one the person saw; a stale one is
    `412 version_conflict`. A level the type does not allow is `422 autonomy_not_allowed`; a window outside 1 to 60 is
    `422 validation_failed`."""
    current = view(conn, user_id)
    if version is not None and version != current.version:
        raise ApiError(
            412, "version_conflict", "The settings changed; load them again.", {"current_version": current.version}
        )
    for kind, level in (levels or {}).items():
        if kind not in ALLOWED_LEVELS:
            raise ApiError(
                422, "validation_failed", "Unknown action type.", {"fields": [{"field": kind, "issue": "unknown"}]}
            )
        if level not in ALLOWED_LEVELS[kind]:
            raise ApiError(
                422,
                "autonomy_not_allowed",
                "That level is not allowed for this kind of action.",
                {"action_type": kind, "level": level, "allowed": list(ALLOWED_LEVELS[kind])},
            )
    low, high = UNDO_WINDOW_RANGE
    if undo_window_minutes is not None and not low <= undo_window_minutes <= high:
        raise ApiError(
            422,
            "validation_failed",
            "The undo window is 1 to 60 minutes.",
            {"fields": [{"field": "undo_window_minutes", "issue": "out_of_range"}]},
        )
    for kind, level in (levels or {}).items():
        conn.execute(
            text(
                """INSERT INTO autonomy_settings (user_id, action_type, level)
                   VALUES (:u, CAST(:t AS action_type), CAST(:l AS autonomy_level))
                   ON CONFLICT (user_id, action_type) DO UPDATE SET level = EXCLUDED.level, updated_at = now()"""
            ),
            {"u": user_id, "t": kind, "l": level},
        )
    if undo_window_minutes is not None:
        conn.execute(
            text("UPDATE users SET undo_window_minutes = :m WHERE id = :u"), {"m": undo_window_minutes, "u": user_id}
        )
    return view(conn, user_id)
