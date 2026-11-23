"""The task job.requirements.extract: render the request, ask the gateway, validate the answer (AI_SYSTEM 2.2, 4.3).

The gateway has no task registry: a task is its name in config/models.toml (the route: model, cap, timeout, data class)
plus what its caller passes to complete(): the output schema, the messages and the prompt version. This module is the
caller. The data class is public (job text), the call has no tools, and no person is charged for a posting (a role
description read for an employer is recorded against that employer: see run()).
"""

import hashlib
from pathlib import Path
from typing import cast
from uuid import UUID

from app.platform.config import Role
from app.platform.llm import SchemaInvalid, complete

from .schema import RequirementList
from .segment import Segment, segments
from .validate import ValidationResult, output_acceptable, to_rows

TASK = "job.requirements.extract"
PROMPT = (Path(__file__).parent / "prompts" / "extract.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)
ATTEMPTS = 2  # one fresh call when no item of an answer survives validation (AI_SYSTEM 2.4)


def render(found: list[Segment]) -> list[dict[str, str]]:
    """The request: the instructions, then the posting as "[n] text" lines."""
    lines = "\n".join(f"[{segment.n}] {segment.text}" for segment in found)
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": lines}]


def run(description: str, *, principal: UUID | None = None, ledger: Role = "worker") -> ValidationResult:
    """Rows for one posting, with spans computed by code. Raises AIDisabled when no model route is usable and
    ProviderUnavailable when the provider fails, both from the gateway. Raises SchemaInvalid when the answer is
    invalid twice, or when it twice proposes items and none survives validation.

    A posting is read in the worker and charged to no one. A role description is read in the API process (hiring
    intake), whose login may record only the signed-in person's calls, so it passes the employer and ledger="api".

    ponytail: the gateway checks only the schema, so the validator's retry lives here and the ledger records the
    unusable answer as ok. A validate hook on complete() would put it in the ledger too."""
    found = segments(description)
    if not found:
        return ValidationResult([], {})  # nothing to read, nothing to pay for
    messages = render(found)
    for _ in range(ATTEMPTS):
        answer = complete(
            TASK, RequirementList, messages, principal=principal, prompt_version=PROMPT_VERSION, ledger=ledger
        )
        result = to_rows(description, found, cast(RequirementList, answer))
        if output_acceptable(result):
            return result
    raise SchemaInvalid()
