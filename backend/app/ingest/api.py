"""The only ingest file other modules may import: what the job index offers the rest of the application.

Plans that add a public function (search, get_job, verify_live, the requirement helpers) add it here; nothing else in
app.ingest is part of that interface.
"""

from .normalize import parse_location

__all__ = ["parse_location"]
