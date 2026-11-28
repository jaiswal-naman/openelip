"""What other modules may import from app.tailor (ARCHITECTURE 3.3, rule 1). Nothing else in this package is a public
interface.

  Fact, Requirement             what tailoring reads: a usable fact, and one requirement of a job
  build_base, apply_edits       the base resume built from facts, and the document after approved edits
  resume_blocks, render_pdf,    a document as blocks, as a PDF (UnsupportedCharacters for a character its embedded font
  render_docx, Block            cannot draw) and as a DOCX; Block is also what a packet's pages are made of
  propose_and_verify            the edits for one job, each passed or discarded with its reason
  unsupported_reason, entailed  layer 1 and layer 2 for free text, such as a packet's sentences
  derive_op                     the op a stored edit row stands for
  request, get, latest_ready_for_job, export_doc, base_doc
                                a tailored resume asked for, read, found for a job, and drawn from approved edits only
  generate                      run the generation of one tailored resume now, on a worker connection of its own (a
                                no-op unless it is still `generating`), for a caller that cannot wait for the queue
  to_fact, require_ai, open_job, rate_limited, fact_text
                                what packets share with tailoring: a usable fact as tailoring reads it, the consent and
                                provider checks, the open-job check and the rolling 24-hour count

No model call or database access is exported that a caller could use to skip the checks.
"""

from uuid import UUID

from app.platform import db

from . import service
from .export import Block, UnsupportedCharacters, render_docx, render_pdf, resume_blocks
from .pipeline import (
    ENTAIL_VERSION,
    PROPOSE_VERSION,
    Claim,
    Proposal,
    VerifiedEdit,
    entailed,
    fact_text,
    propose_and_verify,
)
from .resume import SECTIONS, Bullet, Fact, ResumeDoc, RoleBlock, apply_edits, build_base, derive_op
from .service import (
    base_doc,
    export_doc,
    get,
    latest_ready_for_job,
    open_job,
    rate_limited,
    request,
    require_ai,
    to_fact,
)
from .verifier import REASONS, Requirement, unsupported_reason

__all__ = [
    "ENTAIL_VERSION",
    "PROPOSE_VERSION",
    "REASONS",
    "SECTIONS",
    "Block",
    "Bullet",
    "Claim",
    "Fact",
    "Proposal",
    "Requirement",
    "ResumeDoc",
    "RoleBlock",
    "UnsupportedCharacters",
    "VerifiedEdit",
    "apply_edits",
    "base_doc",
    "build_base",
    "derive_op",
    "export_doc",
    "fact_text",
    "entailed",
    "generate",
    "get",
    "latest_ready_for_job",
    "open_job",
    "propose_and_verify",
    "rate_limited",
    "render_docx",
    "render_pdf",
    "request",
    "require_ai",
    "resume_blocks",
    "to_fact",
    "unsupported_reason",
]


def generate(tailored_resume_id: UUID) -> None:
    """Run the generation of one tailored resume now, in its own worker transaction. A tailored resume that is not
    `generating` (done, failed or gone) is left alone: nothing is called and nothing is written, so a caller that is
    started twice, or after the queue's own job ran, repeats nothing. Raises what the queue's handler raises
    (NonRetryable for consent, a disabled model or an answer that stays invalid; ProviderUnavailable to be retried)."""
    with db.transaction(role="worker") as conn:
        service.generate(conn, {"tailored_resume_id": str(tailored_resume_id)})
