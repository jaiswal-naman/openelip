"""Shared API pieces: the response envelope (API.md 1.3) and the liveness check (API.md 2.1)."""
from fastapi import APIRouter

router = APIRouter()


def envelope(data, **meta):
    return {"success": True, "data": data, "error": None, "meta": meta}


@router.get("/healthz")
def healthz():
    return {"ok": True}
