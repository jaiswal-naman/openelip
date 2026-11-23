"""The shapes of the chat agent: what the model may answer, and what a tool hands back (plan 05-03; AI_SYSTEM 4.8, 5.2).

ChatStep is one model turn: tool calls to run, or the final reply. ProposalList is the answer of record.propose.
Both are closed objects whose optional keys default to None, and neither holds a numeric or length bound or a regular
expression in its JSON schema: the provider refuses such a schema (see ingest/requirements/schema.py), so those rules
are checked here in code after the answer is parsed. A union is a plain `anyOf`, never `oneOf`, for the same reason.

No tool takes a user id. The person is the signed-in session's and reaches a tool as its first argument, so a model
cannot name anyone else's data: `extra="forbid"` refuses a step that tries, and an alias that no tool returned in the
session resolves to nothing (refs.py).
"""

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TOOL_CALLS_PER_STEP = 4  # assumption: AI_SYSTEM 5.3 sets 8 calls for a whole message
MAX_SEARCH_RESULTS = 10  # assumption: what one search_jobs call may return
# d decision, j job, f fact, c constraint, then a number. ponytail: four digits, so 9,999 aliases of a kind in one
# session; a session that long would need a wider pattern here and in refs.py.
ALIAS = re.compile(r"[dfjc][0-9]{1,4}")


def _alias(value: str) -> str:
    if not ALIAS.fullmatch(value):
        raise ValueError("not an alias")
    return value


# The pattern is checked here, not written into the schema (see the module note). The description says what it is.
AliasText = Annotated[
    str, AfterValidator(_alias), Field(description="an alias a tool result showed, such as d1, j2, f3 or c1")
]


class _Call(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetRecord(_Call):
    tool: Literal["get_record"]


class ListMatches(_Call):
    tool: Literal["list_matches"]
    outcome: Literal["shown", "withheld", "filtered"] | None = None
    since: date | None = None
    until: date | None = None


class GetDecision(_Call):
    tool: Literal["get_decision"]
    decision: AliasText | None = None
    job: AliasText | None = None

    @model_validator(mode="after")
    def _names_something(self) -> Self:
        if self.decision is None and self.job is None:
            raise ValueError("get_decision needs a decision or a job")
        return self


class SearchJobs(_Call):
    tool: Literal["search_jobs"]
    q: str
    country: str | None = None
    city: str | None = None
    remote: bool | None = None
    limit: int = 5

    @field_validator("country")
    @classmethod
    def _two_letters(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z]{2}", value):
            raise ValueError("country is two letters")
        return value

    @field_validator("limit")
    @classmethod
    def _at_most_ten(cls, value: int) -> int:
        """A request for more is held to the cap, not refused: a failed step costs a model call, and asking for 20
        results is no attack."""
        return max(1, min(value, MAX_SEARCH_RESULTS))


class GetJob(_Call):
    tool: Literal["get_job"]
    job: AliasText


class GetApplications(_Call):
    tool: Literal["get_applications"]


class ProposeRecordChange(_Call):
    tool: Literal["propose_record_change"]


ToolCall = GetRecord | ListMatches | GetDecision | SearchJobs | GetJob | GetApplications | ProposeRecordChange


class ChatStep(BaseModel):
    """One model turn: at most four tool calls, or the reply, exactly one of the two."""

    model_config = ConfigDict(extra="forbid")
    tool_calls: list[ToolCall] = []
    reply: str | None = None

    @model_validator(mode="after")
    def _calls_or_a_reply(self) -> Self:
        has_reply = bool(self.reply and self.reply.strip())
        if has_reply == bool(self.tool_calls):
            raise ValueError("a step has tool calls or a reply: not both, not neither")
        if len(self.tool_calls) > MAX_TOOL_CALLS_PER_STEP:
            raise ValueError("too many tool calls in one step")
        return self


# ---- record proposals (AI_SYSTEM 4.8) ------------------------------------------------------------------------------


class SalaryFloor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int  # whole currency units a year
    currency: str  # ISO 4217


class ProposalValue(BaseModel):
    """The typed value of a proposal, in the record's own field names. Only the keys of its field are filled in: the
    typed fields of a constraint kind, `text` and `weight` for a preference, `text` for a fact."""

    model_config = ConfigDict(extra="forbid")
    country_codes: list[str] | None = None
    cities: list[str] | None = None
    arrangements: list[str] | None = None
    employment_types: list[str] | None = None
    seniorities: list[str] | None = None
    salary_floor: SalaryFloor | None = None
    company_names: list[str] | None = None
    text: str | None = None
    weight: int | None = None


class ProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: Literal["constraint", "preference", "fact"]
    operation: Literal["set", "remove"]
    field: str  # a constraint kind, a preference kind, or a fact kind: proposals.py holds the allow-list
    value: ProposalValue = Field(default_factory=ProposalValue)
    evidence_quote: str
    statement: str


class ProposalList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposals: list[ProposalItem]


# ---- what the loop passes around -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalEvent:
    """A record change that was proposed (the SSE `proposal` event, API 4.2). `before` is the row it would change and
    `after` what it would write; the record itself is unchanged until the candidate confirms event_id."""

    event_id: UUID
    entity: str  # fact | constraint | preference
    operation: str  # create | update | remove
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    statement: str


@dataclass(frozen=True)
class ToolOutput:
    """What a tool returns to the loop. `data` is what the model reads, and holds aliases only. `refs` holds the real
    ids it concerns by kind (decision, job, fact, constraint), for the event stream and the stored tool row.
    `source_kind` names what the data is in the envelope. Every result is untrusted (it carries posting or resume
    text). A tool that proposes carries its ProposalEvents."""

    data: dict[str, Any]
    refs: dict[str, list[str]] = dataclass_field(default_factory=dict)
    source_kind: str = "tool_result"
    untrusted: bool = True
    proposals: tuple[ProposalEvent, ...] = ()
