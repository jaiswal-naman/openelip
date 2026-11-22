"""Adapters for public, documented job-board endpoints of three applicant tracking systems.

Only these three fixed hosts are ever contacted, so a source token can never
point the fetcher at an internal address.
"""
import re
from datetime import datetime, timezone

import httpx

from .normalize import html_to_text

USER_AGENT = "open-career-agent-indexer/0.1"
MAX_BYTES = 40 * 1024 * 1024
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true",
}


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None


def map_greenhouse(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        if not j.get("id") or not j.get("title") or not j.get("absolute_url"):
            continue
        depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
        out.append({
            "external_id": str(j["id"]),
            "title": j["title"].strip(),
            "location_raw": (j.get("location") or {}).get("name"),
            "country_hint": None,
            "remote_hint": None,
            "employment_type": None,
            "department": depts[0] if depts else None,
            "description": html_to_text(j.get("content")),
            "apply_url": j["absolute_url"],
            "posted_at": _iso(j.get("first_published") or j.get("updated_at")),
        })
    return out


def map_lever(data: list) -> list[dict]:
    out = []
    for j in data if isinstance(data, list) else []:
        if not j.get("id") or not j.get("text") or not j.get("hostedUrl"):
            continue
        cat = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for block in j.get("lists") or []:
            parts.append((block.get("text") or "") + "\n" + html_to_text(block.get("content")))
        parts.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
        workplace = (j.get("workplaceType") or "").lower()
        out.append({
            "external_id": str(j["id"]),
            "title": j["text"].strip(),
            "location_raw": cat.get("location"),
            "country_hint": j.get("country"),
            "remote_hint": True if workplace == "remote" else (False if workplace in ("on-site", "onsite", "hybrid") else None),
            "employment_type": cat.get("commitment"),
            "department": cat.get("department") or cat.get("team"),
            "description": "\n".join(p.strip() for p in parts if p and p.strip()),
            "apply_url": j["hostedUrl"],
            "posted_at": _iso(j.get("createdAt")),
        })
    return out


def map_ashby(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        if not j.get("id") or not j.get("title") or not j.get("jobUrl") or j.get("isListed") is False:
            continue
        postal = ((j.get("address") or {}).get("postalAddress")) or {}
        out.append({
            "external_id": str(j["id"]),
            "title": j["title"].strip(),
            "location_raw": j.get("location"),
            "country_hint": postal.get("addressCountry"),
            "remote_hint": j.get("isRemote") if isinstance(j.get("isRemote"), bool) else None,
            "employment_type": j.get("employmentType"),
            "department": j.get("department") or j.get("team"),
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            "apply_url": j["jobUrl"],
            "posted_at": _iso(j.get("publishedAt")),
        })
    return out


MAPPERS = {"greenhouse": map_greenhouse, "lever": map_lever, "ashby": map_ashby}


async def fetch_postings(client: httpx.AsyncClient, ats: str, token: str):
    """Return (status, http_status, postings, error). status is 'ok' only for HTTP 200 + parseable JSON."""
    if ats not in URLS or not TOKEN_RE.match(token):
        return "parse_error", None, None, "invalid source"
    try:
        r = await client.get(URLS[ats].format(t=token), headers={"User-Agent": USER_AGENT}, timeout=40)
    except httpx.HTTPError as e:
        return "network_error", None, None, type(e).__name__
    if r.status_code != 200:
        return "http_error", r.status_code, None, None
    if len(r.content) > MAX_BYTES:
        return "parse_error", 200, None, "response too large"
    try:
        postings = MAPPERS[ats](r.json())
    except (ValueError, AttributeError, TypeError) as e:
        return "parse_error", 200, None, type(e).__name__
    return "ok", 200, postings, None
