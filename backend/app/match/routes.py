"""HTTP routes of the match module: /v1/matches, /v1/matches/{id}, its feedback, and /v1/decisions (API.md).

No route yet: plan 04-04 adds them to this router, which app.main already mounts.
"""

from fastapi import APIRouter

router = APIRouter()
