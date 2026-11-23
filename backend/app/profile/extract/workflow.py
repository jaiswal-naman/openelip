"""Resume lines to candidate facts: three model calls, every answer checked in code (AI_SYSTEM 4.2; REC-01).

extract_facts sends the numbered lines (contact details masked) to the three resume.facts tasks, checks what comes back
against the resume's own text (validate.py) and returns the facts it can stand behind, plus what it could not confirm.
Raw text is never touched here: a protected line simply does not become a fact (REC-07, owner decision G4).

Failures: a call that is invalid twice is listed in failed_calls and the other two calls still count. When the gateway
cannot call a model at all, its own exception is raised unchanged (AIDisabled: no provider is configured, which the API
answers as 503 ai_disabled; ProviderUnavailable: the provider did not answer), because the caller decides what to tell
the candidate. Logs carry counts only.
"""

import contextvars
import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from app.platform import log
from app.platform.llm import SchemaInvalid
from app.profile.resume.contact import mask

from . import validate
from .schemas import (
    CandidateFact,
    EducationSkillItems,
    RoleItems,
    StatementItems,
    UnverifiedItem,
)

if TYPE_CHECKING:
    from app.profile.resume.reader import ReadResult

PROMPTS = Path(__file__).parent / "prompts"

# app.platform.llm.complete, or anything called the same way: (task, schema, messages, *, principal, prompt_version).
Gateway = Callable[..., Any]
Message = dict[str, str]


@dataclass(frozen=True)
class Task:
    name: str
    schema: type[BaseModel]
    prompt: str
    check: Callable[[list[Any], "ReadResult"], validate.Checked]

    @property
    def version(self) -> str:
        return hashlib.sha256(self.prompt.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)


def _task(name: str, schema: type[BaseModel], prompt_file: str, check: Callable[..., validate.Checked]) -> Task:
    return Task(name, schema, (PROMPTS / prompt_file).read_text(encoding="utf-8"), check)


TASKS = (
    _task("resume.facts.roles", RoleItems, "roles.md", validate.check_roles),
    _task("resume.facts.education_skills", EducationSkillItems, "education_skills.md", validate.check_education_skills),
    _task("resume.facts.summary", StatementItems, "statement.md", validate.check_statements),
)


@dataclass(frozen=True)
class ExtractionResult:
    kept: list[CandidateFact]
    unverified: list[UnverifiedItem]
    protected_skipped: int = 0
    failed_calls: list[str] = field(default_factory=list)  # task names whose answer was invalid twice


def numbered_text(result: "ReadResult") -> str:
    """The lines as the model sees them, "N: text", with email, phone, link and street address replaced (T-3-10)."""
    return "\n".join(f"{line.n}: {mask(line.text).text}" for line in result.lines)


def build_requests(result: "ReadResult") -> list[tuple[Task, list[Message]]]:
    """What is sent for one resume, in the order the calls are made. The recordings are keyed by these (fake.py)."""
    numbered = numbered_text(result)
    return [
        (task, [{"role": "system", "content": task.prompt}, {"role": "user", "content": numbered}]) for task in TASKS
    ]


def prompt_versions() -> dict[str, str]:
    return {task.name: task.version for task in TASKS}


def extract_facts(
    gateway: Gateway, result: "ReadResult", *, attribution: UUID | None, parallel: bool = True
) -> ExtractionResult:
    """attribution is the user the calls are charged to in the ledger, or None for an evaluation. parallel=False makes
    the calls one after another in a fixed order, which the gateway's fake provider needs: it answers in call order."""
    requests = build_requests(result)

    def ask(request: tuple[Task, list[Message]]) -> Any:
        task, messages = request
        try:
            return gateway(task.name, task.schema, messages, principal=attribution, prompt_version=task.version)
        except SchemaInvalid:
            return None

    if parallel:
        with ThreadPoolExecutor(max_workers=len(requests)) as pool:  # leaving the block waits for all three calls
            futures = [pool.submit(contextvars.copy_context().run, ask, request) for request in requests]
            answers = [future.result() for future in futures]
    else:
        answers = [ask(request) for request in requests]

    kept: list[CandidateFact] = []
    unverified: list[UnverifiedItem] = []
    skipped = 0
    failed: list[str] = []
    for (task, _), answer in zip(requests, answers):
        if answer is None:
            failed.append(task.name)
            continue
        checked = task.check(answer.items, result)
        offset = len(kept)  # a parent_index counts within one call's facts; shift it into the merged list
        kept += [
            fact if fact.parent_index is None else replace(fact, parent_index=fact.parent_index + offset)
            for fact in checked.kept
        ]
        unverified += checked.unverified
        skipped += checked.protected_skipped
    log.event(
        "resume.extract",
        kept=len(kept),
        unverified=len(unverified),
        protected_skipped=skipped,
        failed_calls=len(failed),
    )
    return ExtractionResult(kept, unverified, skipped, failed)
