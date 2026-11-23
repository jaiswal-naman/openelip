"""Reading an employer's role description into requirement rows (HIR-02, AI_SYSTEM 4.3).

A role description is read by the same task as a job posting, with the same prompt, route, validator and span rule:
every row's text is the description's own words at its span, in code points, computed by code and never typed by the
model. The employer then edits and confirms the rows before the role can be published (plan 06-03). Role descriptions
are organization text (data class public) and no person is charged for the call, exactly as for a posting.

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

from app.ingest.api import RequirementRow, extract_requirements
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid

State = Literal["extracted", "empty", "ai_disabled", "failed"]


@dataclass(frozen=True)
class Intake:
    rows: list[RequirementRow]
    state: State
    dropped: int = 0  # items beyond the cap the validator keeps (30 per role); the first 30 by position are in rows


def extract(description: str) -> Intake:
    if not description.strip():
        return Intake([], "empty")
    try:
        result = extract_requirements(description)
    except AIDisabled:
        return Intake([], "ai_disabled")
    except (ProviderUnavailable, SchemaInvalid):
        return Intake([], "failed")
    return Intake(result.rows, "extracted", result.rejected.get("over_cap", 0))
