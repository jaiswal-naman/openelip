"""SQLAlchemy 2 definitions of the five Career Record tables (DATA_MODEL 3.4), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The writers build their inserts from these tables. The value lists are the enum types of DATA_MODEL 2.1, written once as
Literal types (the routes use them for the generated API types) with the tuples the services check against.
"""

from typing import Literal, get_args

from sqlalchemy import (
    CHAR,
    BigInteger,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, INT4RANGE, JSONB, UUID

DocumentStatus = Literal["uploaded", "parsing", "parsed", "failed"]
FactKind = Literal["role", "achievement", "education", "skill", "certification", "project", "other"]
RecordSource = Literal["resume", "candidate_edit", "confirmed_proposal"]
ConstraintKind = Literal[
    "location",
    "remote_policy",
    "work_authorization",
    "salary_floor",
    "employment_type",
    "seniority",
    "excluded_company",
]
PreferenceKind = Literal[
    "target_title",
    "target_skill",
    "industry",
    "company_size",
    "location",
    "remote_policy",
    "salary_target",
    "seniority",
    "untyped_dealbreaker",
    "other",
]
ProfileEntity = Literal["fact", "constraint", "preference"]
ProfileOperation = Literal["create", "update", "remove", "confirm", "reject"]
ProfileEventType = Literal["proposed", "applied", "declined", "expired"]
ProposalOrigin = Literal["onboarding", "chat", "feedback", "agent"]
WorkArrangement = Literal["onsite", "hybrid", "remote"]
# What a person may name in a hard constraint. The job index also has 'unknown' ("the posting does not say") in both
# types, which a constraint never holds: the table's CHECK refuses it.
EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary"]
Seniority = Literal["intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive"]

DOCUMENT_STATUSES = get_args(DocumentStatus)
FACT_KINDS = get_args(FactKind)
RECORD_SOURCES = get_args(RecordSource)
CONSTRAINT_KINDS = get_args(ConstraintKind)
PREFERENCE_KINDS = get_args(PreferenceKind)
PROFILE_ENTITIES = get_args(ProfileEntity)
PROFILE_OPERATIONS = get_args(ProfileOperation)
PROFILE_EVENT_TYPES = get_args(ProfileEventType)
PROPOSAL_ORIGINS = get_args(ProposalOrigin)
WORK_ARRANGEMENTS = get_args(WorkArrangement)
EMPLOYMENT_TYPES = get_args(EmploymentType)
SENIORITIES = get_args(Seniority)

# API.md 1.9: the one status vocabulary for long-running work, mapped from the documents table's own.
DOCUMENT_API_STATUS = {"uploaded": "queued", "parsing": "running", "parsed": "succeeded", "failed": "failed"}

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


documents = Table(
    "documents",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("content", LargeBinary, nullable=False),  # the uploaded file as a Fernet token
    Column("original_filename", Text),
    Column("mime_type", Text, nullable=False),
    Column("byte_size", Integer, nullable=False),
    Column("sha256", LargeBinary, nullable=False),
    Column("status", _enum("document_status", DOCUMENT_STATUSES), nullable=False, server_default="uploaded"),
    Column("extracted_text", Text),  # visible text only; fact spans point into this
    Column("hidden_text_chars", Integer, nullable=False, server_default="0"),
    Column("parse_error_code", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("parsed_at", DateTime(timezone=True)),
)

# before and after are SQL NULL when absent, never the JSON value null: the table's CHECK tests for NULL.
profile_events = Table(
    "profile_events",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("event_type", _enum("profile_event_type", PROFILE_EVENT_TYPES), nullable=False),
    Column("entity", _enum("profile_entity", PROFILE_ENTITIES), nullable=False),
    Column("operation", _enum("profile_operation", PROFILE_OPERATIONS), nullable=False),
    Column("entity_id", _uuid()),  # the row created or affected; NULL while only proposed
    Column("before", JSONB(none_as_null=True)),
    Column("after", JSONB(none_as_null=True)),
    Column("source", _enum("record_source", RECORD_SOURCES)),  # applied rows: who made the change
    Column("origin", _enum("proposal_origin", PROPOSAL_ORIGINS)),  # proposed rows: where it came from
    Column("origin_ref", Text),
    Column("proposal_event_id", _uuid()),  # the proposal this row resolves
    Column("reason", Text),
    Column("record_version_after", BigInteger),  # applied rows: users.record_version after the change
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

profile_facts = Table(
    "profile_facts",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("kind", _enum("fact_kind", FACT_KINDS), nullable=False),
    Column("statement", Text, nullable=False),
    Column("attributes", JSONB, nullable=False, server_default=text("'{}'")),
    Column("skill_id", Integer),
    Column("parent_fact_id", _uuid()),
    Column("source", _enum("record_source", RECORD_SOURCES), nullable=False),
    Column("document_id", _uuid()),
    Column("source_span", INT4RANGE),  # half-open character range in documents.extracted_text
    Column("source_quote", Text),  # the text at that range, checked on insert by the database
    Column("profile_event_id", _uuid()),  # non-resume facts: the event that stated them
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    # write-once lifecycle columns
    Column("confirmed_at", DateTime(timezone=True)),
    Column("rejected_at", DateTime(timezone=True)),
    Column("superseded_at", DateTime(timezone=True)),
    Column("superseded_by", _uuid()),
)

constraints = Table(
    "constraints",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("kind", _enum("constraint_kind", CONSTRAINT_KINDS), nullable=False),
    Column("statement", Text, nullable=False),
    Column("country_codes", ARRAY(CHAR(2))),
    Column("cities", ARRAY(Text)),
    Column("arrangements", ARRAY(_enum("work_arrangement", WORK_ARRANGEMENTS))),
    Column("employment_types", ARRAY(_enum("employment_type", (*EMPLOYMENT_TYPES, "unknown")))),
    Column("seniorities", ARRAY(_enum("seniority", (*SENIORITIES, "unknown")))),
    Column("salary_floor", Integer),
    Column("salary_currency", CHAR(3)),
    Column("company_names", ARRAY(Text)),
    Column("source", _enum("record_source", RECORD_SOURCES), nullable=False),
    Column("profile_event_id", _uuid(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("superseded_at", DateTime(timezone=True)),
    Column("superseded_by", _uuid()),
)

preferences = Table(
    "preferences",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("kind", _enum("preference_kind", PREFERENCE_KINDS), nullable=False),
    Column("value", Text, nullable=False),
    Column("skill_id", Integer),
    Column("weight", SmallInteger, nullable=False),
    Column("statement", Text, nullable=False),
    Column("source", _enum("record_source", RECORD_SOURCES), nullable=False),
    Column("profile_event_id", _uuid(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("superseded_at", DateTime(timezone=True)),
    Column("superseded_by", _uuid()),
)
