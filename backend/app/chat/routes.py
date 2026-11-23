"""HTTP routes of the chat module (API.md 2.8: sessions, the transcript, messages, the reply stream and stop).

The router is empty here on purpose: plan 05-01 mounts it in app.main once, and plan 05-04 adds the routes, so no later
plan edits the composition root.
"""

from fastapi import APIRouter

router = APIRouter()
