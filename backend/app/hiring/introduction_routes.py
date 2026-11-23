"""The introduction routes, /v1/introductions, /v1/roles/{id}/shortlist and /v1/orgs/{id}/answer-clock: list, get,
accept, decline, answer, shortlist and the answer clock (HIR-04 to HIR-06). Plan 06-04 fills this router; plan 06-01
made it so app.main includes it once."""

from fastapi import APIRouter

router = APIRouter()
