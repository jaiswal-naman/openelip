"""The organization routes, /v1/orgs: create, get, invite by email, join, change a role, claim ownership, remove or
revoke (HIR-01). Plan 06-03 fills this router; plan 06-01 made it so app.main includes it once."""

from fastapi import APIRouter

router = APIRouter()
