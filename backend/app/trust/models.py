"""SQLAlchemy 2 definitions of the three trust tables (DATA_MODEL 3.3). Definitions only: the migration creates them.

The writers build their inserts from these tables, and check an argument against the enum value lists below before
the database does, so a bad value is a ValueError at the call and not a failed statement that spoils the caller's
transaction. Foreign keys are left to the database; nothing here joins.
"""

from sqlalchemy import BigInteger, Column, DateTime, LargeBinary, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

CONSENT_PURPOSES = (
    "terms",
    "ai_processing",
    "brief_email",
    "open_to_introductions",
    "introduction",
    "agent_access",
)
CONSENT_ACTIONS = ("granted", "withdrawn")
ACTOR_TYPES = ("system", "candidate", "employer", "agent", "outside_agent", "operator")
DATA_REQUEST_KINDS = ("export", "deletion")
DATA_REQUEST_STATUSES = ("open", "in_progress", "completed", "failed")

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


consents = Table(
    "consents",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("purpose", _enum("consent_purpose", CONSENT_PURPOSES), nullable=False),
    Column("action", _enum("consent_action", CONSENT_ACTIONS), nullable=False),
    Column("withdraws_id", _uuid()),  # withdrawal rows: the grant they end
    Column("organization_id", _uuid()),  # purpose 'introduction': the employer
    Column("role_id", _uuid()),  # purpose 'introduction': the role
    Column("text_version", Text, nullable=False),  # version of the wording the person saw
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("event_type", Text, nullable=False),
    Column("actor_type", _enum("actor_type", ACTOR_TYPES), nullable=False),
    Column("actor_user_id", _uuid()),
    Column("subject_user_id", _uuid()),  # the person the event is about
    Column("organization_id", _uuid()),
    Column("agent_token_id", _uuid()),  # set when an outside agent made the call
    Column("entity_table", Text),
    Column("entity_id", Text),
    Column("details", JSONB, nullable=False, server_default=text("'{}'")),
)

data_requests = Table(
    "data_requests",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid()),
    Column("subject_user_id", _uuid(), nullable=False),  # no foreign key: kept after erasure
    Column("kind", _enum("data_request_kind", DATA_REQUEST_KINDS), nullable=False),
    Column("status", _enum("data_request_status", DATA_REQUEST_STATUSES), nullable=False, server_default="open"),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("export_archive", LargeBinary),  # a Fernet token; NULL after 7 days
    Column("export_expires_at", DateTime(timezone=True)),
    Column("store_results", JSONB, nullable=False, server_default=text("'{}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
)
