"""Find which candidate board names really exist, and register the live ones as sources.

A candidate is registered only if its public endpoint answers 200 with at least one posting.
Company names are never invented: Greenhouse names come from its own board endpoint; for
Lever and Ashby, which expose no name, the board name itself is used.

Run:  python -m app.ingest.discover
"""
import asyncio
import sys
from pathlib import Path

import httpx

from app.platform import db

from .adapters import TOKEN_RE, URLS, USER_AGENT, fetch_postings

CANDIDATES = Path(__file__).resolve().parents[3] / "data" / "seed_candidates.txt"
CONCURRENCY = 6


async def greenhouse_name(client: httpx.AsyncClient, token: str) -> str | None:
    try:
        r = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}",
                             headers={"User-Agent": USER_AGENT}, timeout=20)
        return (r.json().get("name") or "").strip() or None if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def run() -> None:
    tokens = sorted({
        line.strip().lower() for line in CANDIDATES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and TOKEN_RE.match(line.strip())
    })
    sem = asyncio.Semaphore(CONCURRENCY)
    found = []

    async with httpx.AsyncClient(follow_redirects=False) as client:
        async def probe(ats: str, token: str):
            async with sem:
                status, _, postings, _ = await fetch_postings(client, ats, token)
                if status != "ok" or not postings:
                    return
                name = (await greenhouse_name(client, token)) if ats == "greenhouse" else None
            found.append((ats, token, name or token, len(postings)))

        await asyncio.gather(*(probe(ats, t) for t in tokens for ats in URLS))

    with db.connect() as conn:
        for ats, token, name, _ in found:
            conn.execute(
                """INSERT INTO job_sources (ats, token, company_name) VALUES (%s, %s, %s)
                   ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name, is_active = true""",
                (ats, token, name),
            )
    by_ats = {a: sum(1 for f in found if f[0] == a) for a in URLS}
    print(f"candidates={len(tokens)} live boards={len(found)} {by_ats} postings={sum(f[3] for f in found)}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
