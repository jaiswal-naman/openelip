"""SQLAlchemy 2 definitions of the two chat tables (DATA_MODEL 3.12), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The value lists are the enum types of DATA_MODEL 2.1 (and the Phase 5 CHECK on finish_reason), written once as Literal
types with the tuples the services check against.
"""

from typing import Literal, get_args

from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

ChatSessionKind = Literal["onboarding", "open"]
ChatRole = Literal["user", "assistant", "tool"]
# How a reply ended (Phase 5, DATA_MODEL 3.12). NULL while it runs, so it is not a member of the list.
FinishReason = Literal["complete", "length", "stopped", "interrupted"]

CHAT_SESSION_KINDS = get_args(ChatSessionKind)
CHAT_ROLES = get_args(ChatRole)
FINISH_REASONS = get_args(FinishReason)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


chat_sessions = Table(
    "chat_sessions",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("kind", _enum("chat_session_kind", CHAT_SESSION_KINDS), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_message_at", DateTime(timezone=True)),
)

chat_messages = Table(
    "chat_messages",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("session_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("role", _enum("chat_role", CHAT_ROLES), nullable=False),
    Column("content", Text),  # user and assistant text; NULL for tool rows
    Column("tool_name", Text),  # tool rows: which tool ran
    Column("refs", JSONB, nullable=False, server_default=text("'{}'::jsonb")),  # ids the message cites
    Column("llm_call_id", BigInteger),
    Column("finish_reason", Text),  # assistant rows only; NULL while the reply runs
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
