"""HTTP routes of the tailor module (API.md 2.9: request, read, decide and export a tailored resume).

The router is empty here on purpose: plan 05-01 mounts it in app.main once, and plan 05-05 adds the routes, so no later
plan edits the composition root.
"""

from fastapi import APIRouter

router = APIRouter()
