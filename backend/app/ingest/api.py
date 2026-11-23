"""The only ingest file other modules may import: what the job index offers the rest of the application.

Plans that add a public function (search, get_job, verify_live, the requirement helpers) add it here; nothing else in
app.ingest is part of that interface.
"""

from .liveness import unchecked_apply_links, verify_live
from .normalize import parse_location
from .requirements.rules import extract as rules_extract
from .requirements.segment import segments
from .requirements.validate import to_rows
from .search import InvalidCursor, SearchPage, get_job, search

__all__ = [
    "InvalidCursor",
    "SearchPage",
    "get_job",
    "parse_location",
    "rules_extract",
    "search",
    "segments",
    "to_rows",
    "unchecked_apply_links",
    "verify_live",
]
