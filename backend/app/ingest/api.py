"""The only ingest file other modules may import: what the job index offers the rest of the application.

Plans that add a public function (search, get_job, verify_live, the requirement helpers) add it here; nothing else in
app.ingest is part of that interface.
"""

from .liveness import verify_live
from .normalize import parse_location
from .requirements.rules import extract as rules_extract
from .requirements.segment import segments
from .requirements.task import run as extract_requirements
from .requirements.validate import RequirementRow, ValidationResult, to_rows
from .search import InvalidCursor, SearchPage, get_job, search

__all__ = [
    "InvalidCursor",
    "RequirementRow",
    "SearchPage",
    "ValidationResult",
    "extract_requirements",
    "get_job",
    "parse_location",
    "rules_extract",
    "search",
    "segments",
    "to_rows",
    "verify_live",
]
