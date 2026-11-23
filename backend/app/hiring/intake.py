"""Reading an employer's role description into requirement rows (HIR-02, AI_SYSTEM 4.3).

A role description is read by the same task as a job posting, with the same prompt, route, validator and span rule:
every row's text is the description's own words at its span, in code points, computed by code and never typed by the
model. The employer then edits and confirms the rows before the role can be published (plan 06-03). Role descriptions
are organization text (data class public). Unlike a posting, which the worker reads and charges to no one, a role is
read inside the employer's request, in the API process, whose login may record only the signed-in person's calls
(llm_calls policy candidate_own): the ledger row names the employer who saved the description.

Every way this can end has a name, so the API can say why there are no rows instead of failing:

  extracted    the task ran; rows may be empty when the text states no requirement
  empty        the text is blank: nothing to read and nothing to pay for, so no request is made
  ai_disabled  no usable model route (no provider, no key, no recording), or a real route no passed eval run
               proves (the same gate as posting extraction and the match gate): nothing was called, the employer
               types the requirements instead
  failed       the provider did not answer, or answered invalid twice: saving the description again tries again

The retry (one fresh request after an answer no item of which survives validation) already lives in the task; nothing
here repeats it. Anything else that goes wrong is a bug and is not caught.
"""

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from app.ingest.api import (
    REQUIREMENTS_PROMPT_VERSION,
    REQUIREMENTS_TASK,
    RequirementRow,
    extract_requirements,
)
from app.platform import config, log
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, registry

# A real route may serve role intake only when a passed gold-set run proves this model with this prompt (AI_SYSTEM
# 2.2): the posting task's run on the test split by the model method, or the role_intake suite's own model run.
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE status = 'passed' AND config->>'method' = 'model'
            AND config->>'model' = :model AND config->>'prompt_version' = :prompt_version
            AND ((suite = 'requirement_extraction' AND config->>'split' = 'test') OR suite = 'role_intake'))"""
)

# assumption: a role has at most 30 requirements (06-INDEX). The validator caps a posting at 30 as well, which is a
# limit of its own; this one is the role's, so it holds if the posting cap ever moves. Plan 06-03 applies it to edits.
MAX_REQUIREMENTS = 30

State = Literal["extracted", "empty", "ai_disabled", "failed"]


@dataclass(frozen=True)
class Intake:
    rows: list[RequirementRow]
    state: State
    dropped: int = 0  # items beyond the role's cap: the first MAX_REQUIREMENTS by position are in rows


def extract(conn: Connection, description: str, caller: UUID) -> Intake:
    if not description.strip():
        return Intake([], "empty")
    try:
        route = registry.route(REQUIREMENTS_TASK, config.get())
    except AIDisabled:
        return Intake([], "ai_disabled")
    if route.adapter != "fake":  # the fake replays recordings (the demo and the tests) and needs no proof
        proof = {"model": route.model, "prompt_version": REQUIREMENTS_PROMPT_VERSION}
        if not conn.execute(ROUTE_IS_PROVEN, proof).scalar_one():
            log.event("hiring.intake", level=logging.WARNING, error_code="unproven", model=route.model)
            return Intake([], "ai_disabled")
    try:
        result = extract_requirements(description, principal=caller, ledger="api")
    except AIDisabled:
        return Intake([], "ai_disabled")
    except (ProviderUnavailable, SchemaInvalid):
        return Intake([], "failed")
    rows = result.rows[:MAX_REQUIREMENTS]
    return Intake(rows, "extracted", result.rejected.get("over_cap", 0) + len(result.rows) - len(rows))
