"""What other modules may import from app.tailor (ARCHITECTURE 3.3, rule 1). Nothing else in this package is a public
interface.

  Fact, Requirement             what tailoring reads: a usable fact, and one requirement of a job
  build_base, apply_edits       the base resume built from facts, and the document after approved edits
  resume_blocks, render_pdf,    a document as blocks, as a PDF (Latin-1 only: UnsupportedCharacters otherwise) and as
  render_docx, Block            a DOCX; Block is also what a packet's pages are made of
  propose_and_verify            the edits for one job, each passed or discarded with its reason
  unsupported_reason, entailed  layer 1 and layer 2 for free text, such as a packet's sentences
  derive_op                     the op a stored edit row stands for
  request, get, latest_ready_for_job, export_doc, base_doc
                                a tailored resume asked for, read, found for a job, and drawn from approved edits only
  to_fact, require_ai, open_job, rate_limited, fact_text
                                what packets share with tailoring: a usable fact as tailoring reads it, the consent and
                                provider checks, the open-job check and the rolling 24-hour count

No model call or database access is exported that a caller could use to skip the checks.
"""

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
