"""The signal routes, /v1/signals: send and list the weekly serious-interest signals (HIR-08). Plan 06-04 fills this
router; plan 06-01 made it so app.main includes it once."""

from fastapi import APIRouter

router = APIRouter()
