"""The role routes, /v1/roles: create with extraction, list, get, patch, requirements, publish, close, preview (HIR-02,
HIR-03). Plan 06-03 fills this router; plan 06-01 made it so app.main includes it once."""

from fastapi import APIRouter

router = APIRouter()
