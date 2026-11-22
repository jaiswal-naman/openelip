"""Find which candidate board names really exist, and register the live ones as sources.

A candidate is registered only if its public endpoint answers 200 with at least one posting.
Company names are never invented: Greenhouse names come from its own board endpoint; for
Lever and Ashby, which expose no name, the board name itself is used.

The network part (find_live_boards) and the database part (register) are separate so the queue job in jobs.py can run
the first with no transaction open. Run it with:  python -m app.cli sources discover
"""

import asyncio
from pathlib import Path

import httpx
from sqlalchemy import Connection, text

from .adapters import TOKEN_RE, URLS, USER_AGENT, fetch_postings

CANDIDATES = Path(__file__).resolve().parents[3] / "data" / "seed_candidates.txt"
CONCURRENCY = 6

Board = tuple[str, str, str, int]  # (ats, token, company name, postings seen)


async def greenhouse_name(client: httpx.AsyncClient, token: str) -> str | None:
    try:
        r = await client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}", headers={"User-Agent": USER_AGENT}, timeout=20
        )
        return (r.json().get("name") or "").strip() or None if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def candidate_tokens() -> list[str]:
    return sorted(
        {
            line.strip().lower()
            for line in CANDIDATES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#") and TOKEN_RE.match(line.strip())
        }
    )


async def find_live_boards(tokens: list[str]) -> list[Board]:
    sem = asyncio.Semaphore(CONCURRENCY)
    found: list[Board] = []

    async with httpx.AsyncClient(follow_redirects=False) as client:

        async def probe(ats: str, token: str) -> None:
            async with sem:
                status, _, postings, _ = await fetch_postings(client, ats, token)
                if status != "ok" or not postings:
                    return
                name = (await greenhouse_name(client, token)) if ats == "greenhouse" else None
            found.append((ats, token, name or token, len(postings)))

        await asyncio.gather(*(probe(ats, t) for t in tokens for ats in URLS))
    return found


def register(conn: Connection, found: list[Board]) -> None:
    for ats, token, name, _ in found:
        conn.execute(
            text(
                """INSERT INTO job_sources (ats, token, company_name) VALUES (:ats, :token, :name)
                   ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name, is_active = true"""
            ),
            {"ats": ats, "token": token, "name": name},
        )
