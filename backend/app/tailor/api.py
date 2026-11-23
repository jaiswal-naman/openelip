"""What other modules may import from app.tailor (ARCHITECTURE 3.3, rule 1). Nothing else in this package is a public
interface.

  Fact, Requirement             what tailoring reads: a usable fact, and one requirement of a job
  build_base, apply_edits       the base resume built from facts, and the document after approved edits
  resume_blocks, render_pdf,    a document as blocks, as a PDF (Latin-1 only: UnsupportedCharacters otherwise) and as
  render_docx, Block            a DOCX; Block is also what a packet's pages are made of
  propose_and_verify            the edits for one job, each passed or discarded with its reason
  unsupported_reason, entailed  layer 1 and layer 2 for free text, such as a packet's sentences
  derive_op                     the op a stored edit row stands for

No model call or database access is exported that a caller could use to skip the checks.
"""

from .export import Block, UnsupportedCharacters, render_docx, render_pdf, resume_blocks
from .pipeline import ENTAIL_VERSION, PROPOSE_VERSION, Claim, Proposal, VerifiedEdit, entailed, propose_and_verify
from .resume import SECTIONS, Bullet, Fact, ResumeDoc, RoleBlock, apply_edits, build_base, derive_op
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
    "build_base",
    "derive_op",
    "entailed",
    "propose_and_verify",
    "render_docx",
    "render_pdf",
    "resume_blocks",
    "unsupported_reason",
]
