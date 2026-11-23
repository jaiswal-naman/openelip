"""What tailor.propose and verify.entail may return (AI_SYSTEM 4.7). Every schema forbids extra fields: a model that
adds a key is giving invalid output, which the gateway asks for once more and then refuses (AI_SYSTEM 2.4).

The JSON schema the provider sees holds only types and closed choices. The rules a schema cannot say (an alias has the
form f12, a reorder lists each item once, one edit per target) are checked here after parsing and fail the same way.
With rephrasing switched off (TAILOR_REPHRASE, AI_SYSTEM 4.7) the model is given EditListSafe, whose `op` has no
rephrase in it, so the closed choice itself keeps the model from proposing one.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

MAX_EDITS = 30  # assumption: more than a person will read, and the model's output cap is 6,000 tokens
MAX_TEXT = 2_000  # profile_facts.statement is checked to 1..2000 characters (DATA_MODEL 3.4)
MAX_ALIASES = 8  # assumption: supporting facts and requirements named by one edit
MAX_ORDER = 60  # assumption: the most skills or bullets one reorder lists

FACT_ALIAS = re.compile(r"f[1-9][0-9]{0,3}")
REQUIREMENT_ALIAS = re.compile(r"r[1-9][0-9]{0,3}")
ENTAIL_ALIAS = re.compile(r"e[1-9][0-9]{0,3}")

Section = Literal["experience", "skills", "education", "certifications", "projects"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# One proposed edit. Keys an operation does not use are null, "" or []. `target`, `supporting` and `order` name facts by
# alias (f1..fN as the request numbered them); nothing here is an id. The classes below carry comments, not docstrings:
# a docstring becomes prose in the JSON schema the provider is sent.
class EditItem(_Strict):
    op: Literal["rephrase", "reorder", "remove", "add_from_fact"]
    target: str | None
    section: Section
    after_text: str
    supporting: list[str]
    requirements: list[str]
    order: list[str]

    @model_validator(mode="after")
    def _shape(self) -> "EditItem":
        _require(len(self.after_text) <= MAX_TEXT, "after_text is too long")
        _require(len(self.supporting) <= MAX_ALIASES and len(self.requirements) <= MAX_ALIASES, "too many aliases")
        _require(len(self.order) <= MAX_ORDER, "order is too long")
        aliases = [self.target] if self.target is not None else []
        _require(all(FACT_ALIAS.fullmatch(a) for a in [*aliases, *self.supporting, *self.order]), "not a fact alias")
        _require(all(REQUIREMENT_ALIAS.fullmatch(a) for a in self.requirements), "not a requirement alias")
        if self.op in ("rephrase", "remove"):
            _require(self.target is not None, f"{self.op} needs a target")
        if self.op in ("rephrase", "add_from_fact"):
            _require(bool(self.after_text.strip()), f"{self.op} needs after_text")
        if self.op == "reorder":
            _require(
                len(self.order) >= 2 and len(set(self.order)) == len(self.order), "reorder needs 2+ distinct items"
            )
        if self.op == "add_from_fact":
            _require(self.target is None and len(self.supporting) == 1, "add_from_fact names exactly one fact")
        return self


# The same item without the reword operation: what the model is offered while TAILOR_REPHRASE is off.
class EditItemSafe(EditItem):
    op: Literal["reorder", "remove", "add_from_fact"]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def edit_key(item: EditItem) -> tuple[str, object]:
    """What an edit changes: a bullet or skill (rephrase, remove), a group of items (reorder) or a fact added."""
    if item.op == "reorder":
        return ("group", frozenset(item.order))
    if item.op == "add_from_fact":
        return ("add", item.supporting[0])
    return ("target", item.target)


def _one_edit_per_target(edits: list[EditItem]) -> None:
    keys = [edit_key(item) for item in edits]
    _require(len(keys) <= MAX_EDITS, "too many edits")
    _require(len(set(keys)) == len(keys), "two edits on one target")


class EditList(_Strict):
    edits: list[EditItem]

    @model_validator(mode="after")
    def _distinct(self) -> "EditList":
        _one_edit_per_target(self.edits)
        return self


class EditListSafe(_Strict):
    edits: list[EditItemSafe]

    @model_validator(mode="after")
    def _distinct(self) -> "EditListSafe":
        _one_edit_per_target(list(self.edits))
        return self


class EntailItem(_Strict):
    edit: str  # e1..eN: the claim's number in the request
    entailed: bool

    @model_validator(mode="after")
    def _alias(self) -> "EntailItem":
        _require(ENTAIL_ALIAS.fullmatch(self.edit) is not None, "not a claim alias")
        return self


class EntailSet(_Strict):
    items: list[EntailItem]
