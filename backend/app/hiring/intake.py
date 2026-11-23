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
  ai_disabled  no usable model route (no provider, no key, no recording): nothing was called, the employer types
               the requirements instead
  failed       the provider did not answer, or answered invalid twice: saving the description again tries again

The retry (one fresh request after an answer no item of which survives validation) already lives in the task; nothing
here repeats it. Anything else that goes wrong is a bug and is not caught.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.ingest.api import RequirementRow, extract_requirements
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid

# assumption: a role has at most 30 requirements (06-INDEX). The validator caps a posting at 30 as well, which is a
# limit of its own; this one is the role's, so it holds if the posting cap ever moves. Plan 06-03 applies it to edits.
MAX_REQUIREMENTS = 30

State = Literal["extracted", "empty", "ai_disabled", "failed"]


@dataclass(frozen=True)
class Intake:
    rows: list[RequirementRow]
    state: State
    dropped: int = 0  # items beyond the role's cap: the first MAX_REQUIREMENTS by position are in rows


def extract(description: str, caller: UUID) -> Intake:
    if not description.strip():
        return Intake([], "empty")
    try:
        result = extract_requirements(description, principal=caller, ledger="api")
    except AIDisabled:
        return Intake([], "ai_disabled")
    except (ProviderUnavailable, SchemaInvalid):
        return Intake([], "failed")
    rows = result.rows[:MAX_REQUIREMENTS]
    return Intake(rows, "extracted", result.rejected.get("over_cap", 0) + len(result.rows) - len(rows))
