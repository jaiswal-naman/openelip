"""SQLAlchemy 2 definitions of the two tailoring tables (DATA_MODEL 3.8), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The value lists are the enum types of DATA_MODEL 2.1, in the type's own order, written once as Literal types with the
tuples the services check against.
"""

from typing import Literal, get_args

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, SmallInteger, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, UUID

TailoredStatus = Literal["generating", "ready", "failed"]
EditVerifierStatus = Literal["passed", "discarded"]
EditDiscardReason = Literal[
    "new_entity",
    "changed_number",
    "changed_date",
    "changed_title",
    "changed_employer",
    "not_entailed",
    "no_supporting_fact",
]
EditDecision = Literal["pending", "approved", "rejected"]

TAILORED_STATUSES = get_args(TailoredStatus)
EDIT_VERIFIER_STATUSES = get_args(EditVerifierStatus)
EDIT_DISCARD_REASONS = get_args(EditDiscardReason)
EDIT_DECISIONS = get_args(EditDecision)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


tailored_resumes = Table(
    "tailored_resumes",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("job_id", BigInteger, nullable=False),
    Column("job_content_version", Integer, nullable=False),
    Column("base_document_id", _uuid(), nullable=False),
    Column("record_version", BigInteger, nullable=False),
    Column("status", _enum("tailored_status", TAILORED_STATUSES), nullable=False, server_default="generating"),
    Column("prompt_version", Text),
    Column("discarded_count", Integer, nullable=False, server_default="0"),  # edits the verifier threw away (TLR-02)
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

resume_edits = Table(
    "resume_edits",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("tailored_resume_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("section", Text, nullable=False),
    Column("target_fact_id", _uuid()),  # the fact being reworded
    Column("before_text", Text, nullable=False),
    Column("after_text", Text, nullable=False),
    Column("supporting_fact_ids", ARRAY(_uuid()), nullable=False, server_default="{}"),  # checked by trigger
    Column("verifier_status", _enum("edit_verifier_status", EDIT_VERIFIER_STATUSES), nullable=False),
    Column("discard_reason", _enum("edit_discard_reason", EDIT_DISCARD_REASONS)),
    Column("decision", _enum("edit_decision", EDIT_DECISIONS), nullable=False, server_default="pending"),
    Column("decided_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
