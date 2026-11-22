"""Find which candidate board names really exist, and register the live ones as sources.

A candidate is registered only if its public endpoint answers 200 with at least one posting.
Company names are never invented: Greenhouse names come from its own board endpoint; for
Lever and Ashby, which expose no name, the board name itself is used.

The network part (find_live_boards) and the database part (register) are separate so the queue job in jobs.py can run
the first with no transaction open. Run it with:  python -m app.cli sources discover

import_boards is the same probe and the same upsert for names the operator supplies (IDX-09):
python -m app.cli sources import <file> [--dry-run]. The seed job and the import share find_live_boards and register,
so a board is registered by the same rule whichever way its name arrived.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ImportResult:
    probed: int  # valid names, each tried against all three board APIs
    registered: int  # live boards that were not in job_sources before (a name live on two APIs counts twice)
    already_known: int  # live boards that were (an inactive one is switched back on)
    rejected: list[str]  # lines that are not board names


def parse_names(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """The valid names (stripped, lower case, one each, sorted) and the lines that are not names. A blank line or a
    # comment is neither."""
    names: set[str] = set()
    rejected: list[str] = []
    for line in lines:
        name = line.strip().lower()
        if not name or name.startswith("#"):
            continue
        if TOKEN_RE.match(name):
            names.add(name)
        else:
            rejected.append(line.strip())
    return sorted(names), rejected


def candidate_tokens() -> list[str]:
    return parse_names(CANDIDATES.read_text(encoding="utf-8").splitlines())[0]


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
                """INSERT INTO job_sources (ats, token, company_name) VALUES (CAST(:ats AS ats_type), :token, :name)
                   ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name, is_active = true"""
            ),
            {"ats": ats, "token": token, "name": name},
        )


def import_boards(conn: Connection, names: Iterable[str], *, dry_run: bool = False) -> ImportResult:
    """Probe every name against the three board APIs and register the boards that answer with at least one posting.

    `names` are the lines of a file as they stand. A dry run probes and counts the same, and writes nothing. The probe
    runs while `conn` sits idle in its transaction (it holds no lock); the board APIs are the only hosts contacted.
    """
    tokens, rejected = parse_names(names)
    found = asyncio.run(find_live_boards(tokens))
    known = {
        (row.ats, row.token)
        for row in conn.execute(
            text("SELECT ats::text AS ats, token FROM job_sources WHERE token = ANY(CAST(:tokens AS text[]))"),
            {"tokens": tokens},
        )
    }
    live = {(ats, token) for ats, token, _, _ in found}
    if not dry_run:
        register(conn, found)
    return ImportResult(len(tokens), len(live - known), len(live & known), rejected)
