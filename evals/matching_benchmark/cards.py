"""Print one profile and a compact card for each of its pooled jobs, for labelling (GUIDELINE.md).

    cd backend
    uv run python ../evals/matching_benchmark/cards.py p01 --database-url postgresql://app:...@127.0.0.1:5433/<name>_test

The cards come in an order that hides which method returned a job (a hash of the profile and the job), and nothing on
a card names a method. Labels are written as {profile_key, job_ref, grade, note, labels} in labels.jsonl.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import target  # noqa: E402

POOL = HERE / "pool.jsonl"
PROFILES = HERE / "profiles.jsonl"
LEAD = 600  # characters of the description on a card


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def profile_block(profile: dict[str, Any]) -> str:
    """The profile as the labeller reads it: roles, skills, education, wishes with their weights, hard constraints."""
    facts = profile["facts"]
    kinds = {
        k: [f["statement"] for f in facts if f["kind"] == k] for k in ("role", "achievement", "skill", "education")
    }
    wishes = "; ".join(f"{p['value']} ({p['weight']:+d})" for p in profile["preferences"])
    rules = " ".join(c["statement"] for c in profile["constraints"]) or "none"
    return "\n".join(
        [
            f"PROFILE {profile['key']}: {profile['headline']}",
            *(f"  role: {s}" for s in kinds["role"]),
            *(f"  achievement: {s}" for s in kinds["achievement"]),
            f"  skills: {', '.join(kinds['skill'])}",
            *(f"  education: {s}" for s in kinds["education"]),
            f"  looking for (weight): {wishes}",
            f"  hard constraints (already applied to every card below): {rules}",
        ]
    )


def arrangement_text(arrangements: list[str]) -> str:
    if not arrangements:
        return "not stated"
    return "on-site or hybrid" if sorted(arrangements) == ["hybrid", "onsite"] else " or ".join(arrangements)


def card(ref: str, row: Any) -> str:
    """One posting: title, company, the place, arrangement, level and pay it states, and the start of its text."""
    place = ", ".join(part for part in (row.city, row.country) if part) or "not stated"
    pay = f"{row.salary_min}-{row.salary_max} {row.salary_currency}" if row.salary_max else "not stated"
    level = row.seniority if row.seniority != "unknown" else "not stated"
    return (
        f"[{ref}] {row.title} - {row.company_name}\n"
        f"  place: {place} | arrangement: {arrangement_text(row.arrangements)} | level: {level} | pay: {pay}\n"
        f"  {' '.join(row.lead.split())}"
    )


def hidden_order(profile_key: str, refs: list[str]) -> list[str]:
    """A fixed order that tells nothing about which method returned a job."""
    return sorted(refs, key=lambda ref: hashlib.sha256(f"{profile_key}/{ref}".encode()).hexdigest())


def main() -> int:
    from sqlalchemy import text

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # postings hold any character; a console may not
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("profile_key")
    ap.add_argument("--database-url", required=True, help="the benchmark database; its name must end in _test")
    args = ap.parse_args()
    url = target.require_scratch(args.database_url)
    target.point_app_at(url)
    from app.platform import db

    profile = next(p for p in read_jsonl(PROFILES) if p["key"] == args.profile_key)
    refs = hidden_order(
        args.profile_key, [r["job_ref"] for r in read_jsonl(POOL) if r["profile_key"] == args.profile_key]
    )
    with db.transaction(role="migrate") as conn:
        by_ref = {ref: job_id for job_id, ref in target.job_refs(conn).items()}
        rows = {
            row.id: row
            for row in conn.execute(
                text(
                    f"""SELECT id, title, company_name, country, city, work_arrangements::text[] AS arrangements,
                               seniority::text AS seniority, salary_min, salary_max, salary_currency,
                               left(description, {LEAD}) AS lead
                          FROM jobs WHERE id = ANY(:ids)"""
                ),
                {"ids": [by_ref[ref] for ref in refs]},
            )
        }
    print(profile_block(profile))
    print(f"\n{len(refs)} postings to grade 0 to 3 (GUIDELINE.md):\n")
    print("\n".join(card(ref, rows[by_ref[ref]]) for ref in refs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
