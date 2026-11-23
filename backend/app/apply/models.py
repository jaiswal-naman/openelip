"""SQLAlchemy 2 definitions of the three application tables (DATA_MODEL 3.8), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The value lists are the enum types of DATA_MODEL 2.1, written once as Literal types (the routes use them for the
generated API types) with the tuples the services check against.
"""

from typing import Literal, get_args

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

ApplicationStatus = Literal["applied", "interview", "offer", "rejected", "no_response"]
# 'approved' is never set in Phase 5: approval belongs to the campaign's submit_application action (Phase 7).
PacketStatus = Literal["draft", "ready", "approved", "superseded"]

APPLICATION_STATUSES = get_args(ApplicationStatus)
PACKET_STATUSES = get_args(PacketStatus)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


application_packets = Table(
    "application_packets",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("job_id", BigInteger, nullable=False),
    Column("campaign_id", _uuid()),  # NULL until campaigns exist (Phase 7)
    Column("tailored_resume_id", _uuid()),
    Column("answers", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("cover_note", Text),  # at most 2,000 characters, by CHECK
    Column("status", _enum("packet_status", PACKET_STATUSES), nullable=False, server_default="draft"),
    Column("approved_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

applications = Table(
    "applications",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("job_id", BigInteger, nullable=False),
    Column("campaign_id", _uuid()),  # NULL until campaigns exist (Phase 7)
    Column("application_packet_id", _uuid()),
    Column("status", _enum("application_status", APPLICATION_STATUSES), nullable=False),  # the latest event's
    Column("status_changed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

# The status history. Append-only.
application_events = Table(
    "application_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("application_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("status", _enum("application_status", APPLICATION_STATUSES), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),  # when the candidate says it happened
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
