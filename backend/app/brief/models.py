"""SQLAlchemy 2 definitions of the two brief tables (DATA_MODEL 3.7), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins. The outbox
table the brief hands its email to belongs to the platform.

The value lists are the enum types of DATA_MODEL 3.1, written once as Literal types with the tuples the code checks
against.
"""

from typing import Literal, get_args

from sqlalchemy import BigInteger, Column, Date, DateTime, MetaData, SmallInteger, Table, text
from sqlalchemy.dialects.postgresql import ENUM, UUID

BriefChannel = Literal["email", "in_app"]
BriefStatus = Literal["composed", "suppressed", "queued", "sent", "failed"]
BriefItemKind = Literal["match", "approval", "activity", "introduction"]

BRIEF_CHANNELS = get_args(BriefChannel)
BRIEF_STATUSES = get_args(BriefStatus)
BRIEF_ITEM_KINDS = get_args(BriefItemKind)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


briefs = Table(
    "briefs",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("local_date", Date, nullable=False),
    Column("channel", _enum("brief_channel", BRIEF_CHANNELS), nullable=False),
    Column("status", _enum("brief_status", BRIEF_STATUSES), nullable=False, server_default="composed"),
    Column("match_run_id", _uuid()),
    Column("covers_since", DateTime(timezone=True)),  # "done since the last brief" starts here (BRF-02)
    Column("send_after", DateTime(timezone=True)),  # planned send time, before 07:00 local
    Column("sent_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

# Exactly one of the four ids is set, the one the kind names (the table's CHECKs).
brief_items = Table(
    "brief_items",
    metadata,
    Column("brief_id", _uuid(), primary_key=True),
    Column("position", SmallInteger, primary_key=True),
    Column("user_id", _uuid(), nullable=False),
    Column("kind", _enum("brief_item_kind", BRIEF_ITEM_KINDS), nullable=False),
    Column("match_decision_id", _uuid()),
    Column("approval_id", _uuid()),
    Column("agent_action_id", BigInteger),
    Column("introduction_id", _uuid()),
)
