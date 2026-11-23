"""SQLAlchemy 2 definitions of the four match tables (DATA_MODEL 3.6), and the closed vocabularies they use.
Definitions only: the migration creates them, foreign keys are left to the database and nothing here joins.

The writers build their inserts from these tables. The value lists are the enum types of DATA_MODEL 3.1, written once as
Literal types (the routes use them for the generated API types) with the tuples the services check against.
"""

from typing import Literal, get_args

from sqlalchemy import REAL, BigInteger, Column, Date, DateTime, Integer, MetaData, SmallInteger, Table, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, UUID

MatchTrigger = Literal["nightly", "onboarding", "on_demand", "role"]
MatchRunStatus = Literal["running", "completed", "failed"]
MatchOutcome = Literal["shown", "introduced", "withheld", "filtered"]
MatchReason = Literal[
    "top_ranked",
    "material_change",
    "selected_for_role",
    "weak_fit",
    "required_not_met",
    "below_daily_cap",
    "citation_unresolved",
    "already_shown",
    "job_closed",
    "liveness_unverified",
    "not_scored",
    "not_retrieved",
    "hard_constraint",
    "injection_suspected",
]
UnverifiedFlag = Literal[
    "location_not_stated",
    "arrangement_not_stated",
    "arrangement_ambiguous",
    "salary_not_stated",
    "salary_currency_differs",
    "employment_type_not_stated",
    "seniority_not_stated",
]
EvidenceVerdict = Literal["met", "partly_met", "not_met", "no_evidence"]
FeedbackKind = Literal["save", "unsave", "dismiss"]
DismissReason = Literal[
    "not_interested_role",
    "not_interested_company",
    "seniority_mismatch",
    "compensation",
    "location",
    "skills_mismatch",
    "already_applied",
    "explanation_wrong",
    "duplicate",
    "job_closed",
    "other",
]
# The Career Record's type (profile.models owns it, and only profile.api may be imported from outside): the same seven
# values, because a filtered decision names the constraints a job broke.
ConstraintKind = Literal[
    "location",
    "remote_policy",
    "work_authorization",
    "salary_floor",
    "employment_type",
    "seniority",
    "excluded_company",
]

MATCH_TRIGGERS = get_args(MatchTrigger)
MATCH_RUN_STATUSES = get_args(MatchRunStatus)
MATCH_OUTCOMES = get_args(MatchOutcome)
MATCH_REASONS = get_args(MatchReason)
UNVERIFIED_FLAGS = get_args(UnverifiedFlag)
EVIDENCE_VERDICTS = get_args(EvidenceVerdict)
FEEDBACK_KINDS = get_args(FeedbackKind)
DISMISS_REASONS = get_args(DismissReason)
CONSTRAINT_KINDS = get_args(ConstraintKind)

metadata = MetaData()


def _enum(name: str, values: tuple[str, ...]) -> ENUM:
    return ENUM(*values, name=name, create_type=False)  # the migration made the type


def _uuid() -> UUID:
    return UUID(as_uuid=True)


match_runs = Table(
    "match_runs",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("trigger", _enum("match_trigger", MATCH_TRIGGERS), nullable=False),
    Column("user_id", _uuid()),  # NULL for trigger 'role'
    Column("role_id", _uuid()),  # trigger 'role' only
    Column("local_date", Date, nullable=False),  # in the candidate's time zone
    Column("status", _enum("match_run_status", MATCH_RUN_STATUSES), nullable=False, server_default="running"),
    Column("record_version", BigInteger),  # candidate runs: the record version read
    Column("since", DateTime(timezone=True)),  # jobs changed after this were new; NULL = whole index
    Column("ranker_version", Text, nullable=False),
    Column("evidence_prompt_version", Text),  # NULL for a run that did no scoring
    Column("retrieved_count", Integer, nullable=False, server_default="0"),
    Column("filtered_count", Integer, nullable=False, server_default="0"),  # every job the constraints removed
    Column("shown_count", SmallInteger, nullable=False, server_default="0"),
    Column("error_code", Text),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("finished_at", DateTime(timezone=True)),
)

match_decisions = Table(
    "match_decisions",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("match_run_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("job_id", BigInteger, nullable=False),
    Column("job_content_version", Integer, nullable=False),
    Column("local_date", Date, nullable=False),
    Column("outcome", _enum("match_outcome", MATCH_OUTCOMES), nullable=False),
    Column("reason_codes", ARRAY(_enum("match_reason", MATCH_REASONS)), nullable=False),
    Column("violated_kinds", ARRAY(_enum("constraint_kind", CONSTRAINT_KINDS)), nullable=False, server_default="{}"),
    Column("unverified", ARRAY(_enum("unverified_flag", UNVERIFIED_FLAGS)), nullable=False, server_default="{}"),
    Column("retrieval_rank", Integer),
    Column("rrf_score", REAL),
    Column("fit_score", REAL),  # rules ranker; NULL if not scored
    Column("rank_position", SmallInteger),  # 1 to 5 for 'shown' rows
    Column("summary", Text),  # one sentence for 'shown' and 'introduced'
    Column("change_note", Text),  # reason 'material_change'
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

match_evidence = Table(
    "match_evidence",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("match_decision_id", _uuid(), nullable=False),
    Column("user_id", _uuid(), nullable=False),
    Column("job_requirement_id", BigInteger, nullable=False),
    Column("verdict", _enum("evidence_verdict", EVIDENCE_VERDICTS), nullable=False),
    Column("fact_ids", ARRAY(_uuid()), nullable=False, server_default="{}"),  # profile_facts cited; checked by trigger
    Column("note", Text),
    Column("llm_call_id", BigInteger),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

feedback_events = Table(
    "feedback_events",
    metadata,
    Column("id", _uuid(), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("user_id", _uuid(), nullable=False),
    Column("job_id", BigInteger, nullable=False),
    Column("match_decision_id", _uuid()),
    Column("kind", _enum("feedback_kind", FEEDBACK_KINDS), nullable=False),
    Column("dismiss_reason", _enum("dismiss_reason", DISMISS_REASONS)),
    Column("comment", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
