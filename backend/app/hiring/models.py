"""SQLAlchemy 2 definitions of the seven hiring tables (DATA_MODEL 3.9), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The writers build their inserts from these tables. The value lists are the enum types of DATA_MODEL 3.1, written once as
Literal types (the routes use them for the generated API types) with the tuples the services check against. The last six
are the job index's types, which a role shares with a job (this module may not import ingest's models).
"""

from typing import Literal, get_args

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, INT4RANGE, JSONB, UUID

OrgRole = Literal["owner", "admin", "member"]
MemberStatus = Literal["invited", "active", "removed"]
RoleStatus = Literal["draft", "open", "closed"]
IntroductionStatus = Literal["asked", "accepted", "declined", "expired", "withdrawn"]
EmployerAnswer = Literal["yes", "no"]
SignalSide = Literal["candidate", "organization"]
WorkArrangement = Literal["onsite", "hybrid", "remote"]
EmploymentType = Literal["full_time", "part_time", "contract", "internship", "temporary", "unknown"]
Seniority = Literal[
    "intern", "entry", "mid", "senior", "staff", "principal", "manager", "director", "executive", "unknown"
]
VisaSponsorship = Literal["offered", "not_offered", "unknown"]
RequirementImportance = Literal["required", "preferred"]
RequirementKind = Literal["skill", "experience", "education", "certification", "authorization", "location", "other"]

ORG_ROLES = get_args(OrgRole)
MEMBER_STATUSES = get_args(MemberStatus)
ROLE_STATUSES = get_args(RoleStatus)
INTRODUCTION_STATUSES = get_args(IntroductionStatus)
EMPLOYER_ANSWERS = get_args(EmployerAnswer)
SIGNAL_SIDES = get_args(SignalSide)
WORK_ARRANGEMENTS = get_args(WorkArrangement)
EMPLOYMENT_TYPES = get_args(EmploymentType)
SENIORITIES = get_args(Seniority)
VISA_SPONSORSHIPS = get_args(VisaSponsorship)
REQUIREMENT_IMPORTANCES = get_args(RequirementImportance)
REQUIREMENT_KINDS = get_args(RequirementKind)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


def _timestamptz() -> DateTime:
    return DateTime(timezone=True)


organizations = Table(
    "organizations",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, nullable=False),
    Column("name_key", Text, Computed("company_key(name)", persisted=True)),  # compared with candidates' blocks
    Column("timezone", Text, nullable=False, server_default="UTC"),  # business days for the answer clock
    Column("is_synthetic", Boolean, nullable=False, server_default="false"),
    Column("created_by", _uuid()),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
)

org_members = Table(
    "org_members",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("organization_id", _uuid(), nullable=False),
    Column("user_id", _uuid()),  # NULL while it is an invitation
    Column("invited_email", Text),
    Column("role", _enum("org_role", ORG_ROLES), nullable=False, server_default="member"),
    Column("status", _enum("member_status", MEMBER_STATUSES), nullable=False, server_default="invited"),
    Column("invited_by", _uuid()),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
    Column("joined_at", _timestamptz()),
    Column("removed_at", _timestamptz()),
)

roles = Table(
    "roles",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("organization_id", _uuid(), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("status", _enum("role_status", ROLE_STATUSES), nullable=False, server_default="draft"),
    Column("country", CHAR(2)),
    Column("city", Text),
    Column(
        "work_arrangements", ARRAY(_enum("work_arrangement", WORK_ARRANGEMENTS)), nullable=False, server_default="{}"
    ),
    Column("employment_type", _enum("employment_type", EMPLOYMENT_TYPES), nullable=False, server_default="unknown"),
    Column("seniority", _enum("seniority", SENIORITIES), nullable=False, server_default="unknown"),
    Column("salary_min", Integer),
    Column("salary_max", Integer),
    Column("salary_currency", CHAR(3)),
    Column("visa_sponsorship", _enum("visa_sponsorship", VISA_SPONSORSHIPS), nullable=False, server_default="unknown"),
    Column("job_id", BigInteger),  # the unlisted index row it is published as
    Column("requirements_confirmed_at", _timestamptz()),
    Column("created_by", _uuid()),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
    Column("opened_at", _timestamptz()),
    Column("closed_at", _timestamptz()),
)

role_requirements = Table(
    "role_requirements",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("role_id", _uuid(), nullable=False),
    Column("organization_id", _uuid(), nullable=False),
    Column("ordinal", SmallInteger, nullable=False),
    Column("importance", _enum("requirement_importance", REQUIREMENT_IMPORTANCES), nullable=False),
    Column("kind", _enum("requirement_kind", REQUIREMENT_KINDS), nullable=False),
    Column("text", Text, nullable=False),
    Column("span", INT4RANGE),  # in roles.description; NULL when the employer typed the item in
    Column("is_confirmed", Boolean, nullable=False, server_default="false"),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
)

introductions = Table(
    "introductions",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("organization_id", _uuid(), nullable=False),
    Column("role_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),  # the candidate
    Column("match_decision_id", _uuid(), nullable=False),
    Column("status", _enum("introduction_status", INTRODUCTION_STATUSES), nullable=False, server_default="asked"),
    Column("share_preview", JSONB, nullable=False),  # exactly what the employer receives on acceptance
    Column("asked_at", _timestamptz(), nullable=False, server_default=text("now()")),
    Column("expires_at", _timestamptz(), nullable=False),  # set by the database: asked_at plus seven days
    Column("responded_at", _timestamptz()),
    Column("answer_due_at", _timestamptz()),  # set on acceptance: five business days later
    Column("employer_answer", _enum("employer_answer", EMPLOYER_ANSWERS)),  # NULL while the employer owes an answer
    Column("employer_answer_reason", Text),
    Column("employer_answered_by", _uuid()),
    Column("employer_answered_at", _timestamptz()),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
)

shortlist_entries = Table(
    "shortlist_entries",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("organization_id", _uuid(), nullable=False),
    Column("role_id", _uuid(), nullable=False),
    Column("introduction_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("shared_payload", JSONB, nullable=False),  # equals introductions.share_preview, checked on insert
    Column("contact_released_at", _timestamptz()),
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
)

interest_signals = Table(
    "interest_signals",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("sender_side", _enum("signal_side", SIGNAL_SIDES), nullable=False),
    Column("user_id", _uuid(), nullable=False),  # the candidate
    Column("organization_id", _uuid(), nullable=False),
    Column("role_id", _uuid(), nullable=False),
    Column("introduction_id", _uuid()),  # organization signals ride on one
    Column("sent_by", _uuid()),
    Column("week_start", Date, nullable=False),  # Monday of the UTC week it was sent
    Column("slot", SmallInteger, nullable=False),  # 1 to 3: the weekly limit is a unique key
    Column("created_at", _timestamptz(), nullable=False, server_default=text("now()")),
)
