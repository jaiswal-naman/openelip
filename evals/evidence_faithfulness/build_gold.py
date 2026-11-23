"""Builds gold.jsonl for the evidence_faithfulness suite from three inputs, and shows the sheet a labeller works from.

    uv run --project backend python evals/evidence_faithfulness/build_gold.py            # write gold.jsonl
    uv run --project backend python evals/evidence_faithfulness/build_gold.py --check    # exit 0 only if it is current
    uv run --project backend python evals/evidence_faithfulness/build_gold.py --show resume-001 req-033 req-025

The inputs: labels.json (the hand-drafted verdicts, written for GUIDELINE.md), the invented resumes of
evals/resume_extraction/facts/gold.jsonl (Phase 3) and the real public postings of
evals/requirement_extraction/gold.jsonl (Phase 2). Nothing else is read, no model is called, no database is touched.

Each resume fact becomes a row shaped like profile.api.usable_facts returns it, and each posting requirement a row
shaped like the "requirements" of ingest.api.get_job, so a gold item is exactly what build_request takes. Facts and
requirements are written in the order build_request numbers them (f1.. and r1..), which the expected verdicts cite.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.match.evidence import build_request

HERE = Path(__file__).parent
EVALS = HERE.parent
RESUMES = EVALS / "resume_extraction" / "facts" / "gold.jsonl"
POSTINGS = EVALS / "requirement_extraction" / "gold.jsonl"
LABELS = HERE / "labels.json"
GOLD = HERE / "gold.jsonl"

AS_OF = date(2026, 11, 23)  # where a current role ends: the verdicts were judged against lengths counted to this day
CREATED = "2026-11-01T09:00:00+00:00"  # every fact of a resume was "confirmed" together, so aliases follow the text
LABEL_MARKER = "agent-drafted, maintainer review pending"
CURRENT = ("present", "current", "now", "ongoing", "today")  # an end_text that means the role has not ended


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _span_of(resume: dict[str, Any], fact: dict[str, Any], starts: list[int]) -> tuple[int, int]:
    if "span" in fact:  # a skill: its own words inside the line
        return fact["span"][0], fact["span"][1]
    last = fact["line_end"] - 1
    return starts[fact["line_start"] - 1], starts[last] + len(resume["lines"][last])


def _attributes(fact: dict[str, Any]) -> dict[str, Any]:
    """What the profile module stores in profile_facts.attributes for each kind of fact."""
    kind = fact["kind"]
    names = {
        "role": ("employer", "title", "start_date", "end_date"),
        "project": ("title", "start_date", "end_date"),
        "education": ("institution", "degree", "start_date", "end_date"),
        "certification": ("name", "end_date"),
        "skill": ("name",),
    }.get(kind, ())
    attributes = {name: fact[name] for name in names if fact.get(name)}
    if kind == "role" and not fact.get("end_date") and str(fact.get("end_text", "")).lower() in CURRENT:
        attributes["is_current"] = True
    if kind == "other":
        attributes["section"] = fact["label"]
    return attributes


def resume_facts(resume: dict[str, Any]) -> list[dict[str, Any]]:
    """The resume's truth as usable_facts rows, in the truth's own order (the index a label cites)."""
    text = "\n".join(resume["lines"])
    starts, at = [], 0
    for line in resume["lines"]:
        starts.append(at)
        at += len(line) + 1
    document = str(uuid5(NAMESPACE_URL, f"openelip/evidence-gold/{resume['key']}"))
    ids = [
        str(uuid5(NAMESPACE_URL, f"openelip/evidence-gold/{resume['key']}/{n}"))
        for n in range(len(resume["expected"]["facts"]))
    ]
    rows = []
    for n, fact in enumerate(resume["expected"]["facts"]):
        start, end = _span_of(resume, fact, starts)
        parent = fact.get("parent")
        rows.append(
            {
                "id": ids[n],
                "kind": fact["kind"],
                "status": "confirmed",
                "statement": text[start:end],
                "attributes": _attributes(fact),
                "source": "resume",
                "parent_fact_id": ids[parent] if parent is not None else None,
                "provenance": {"document_id": document, "span": {"start": start, "end": end}, "quote": text[start:end]},
                "created_at": CREATED,
            }
        )
    return rows


def posting_requirements(posting: dict[str, Any]) -> list[dict[str, Any]]:
    """The posting's labelled items as get_job's requirement rows. The ids are made up: posting number, then ordinal."""
    number = int(posting["posting_id"].split("-")[1])
    return [
        {
            "id": number * 100 + n,
            "ordinal": n,
            "text": item["text"],
            "importance": item["importance"],
            "kind": item["kind"],
            "span": {"start": item["start"], "end": item["end"]},
            "min_years": item["min_years"],
        }
        for n, item in enumerate(posting["requirements"], 1)
    ]


def slice_of(requirements: list[dict[str, Any]]) -> str:
    """The pair's requirement kind mix: led by skills, led by experience, or mixed."""
    for kind in ("skill", "experience"):
        if sum(r["kind"] == kind for r in requirements) * 2 >= len(requirements):
            return f"{kind}_led"
    return "mixed"


def build(labels: dict[str, Any], resumes: dict[str, Any], postings: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for pair in labels["pairs"]:
        resume, posting = resumes[pair["resume"]], postings[pair["posting"]]
        truth = resume_facts(resume)
        requirements = posting_requirements(posting)
        request = build_request(truth, requirements, description=posting["description"], today=AS_OF)
        alias_of_fact = {ref.id: alias for alias, ref in request.facts.items()}
        by_id = {row["id"]: row for row in truth}
        verdicts = []
        for ordinal, requirement in enumerate(requirements, 1):
            verdict, cited, why = pair["verdicts"][str(ordinal)]
            verdicts.append(
                {
                    "requirement": f"r{ordinal}",
                    "verdict": verdict,
                    "facts": sorted((alias_of_fact[truth[n]["id"]] for n in cited), key=lambda a: int(a[1:])),
                    "why": why,
                }
            )
        items.append(
            {
                "key": pair["key"],
                "slice": slice_of(requirements),
                "fit": pair["fit"],
                "resume_ref": pair["resume"],
                "posting_ref": pair["posting"],
                "as_of": AS_OF.isoformat(),
                "facts": [by_id[ref.id] for ref in request.facts.values()],
                "requirements": requirements,
                "expected": {"verdicts": verdicts},
                "labels": LABEL_MARKER,
            }
        )
    return items


def render(items: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    labels = json.loads(LABELS.read_text(encoding="utf-8"))
    resumes = {row["key"]: row for row in read_jsonl(RESUMES)}
    postings = {row["posting_id"]: row for row in read_jsonl(POSTINGS)}
    return labels, resumes, postings


def show(resume_key: str, posting_ids: list[str]) -> None:
    """The sheet a labeller works from: the resume's facts by the index a label cites (as the model is shown them), then
    the requirements of each posting by ordinal."""
    _, resumes, postings = load_inputs()
    truth = resume_facts(resumes[resume_key])
    for position, posting_id in enumerate(posting_ids):
        requirements = posting_requirements(postings[posting_id])
        request = build_request(truth, requirements, description=postings[posting_id]["description"], today=AS_OF)
        if position == 0:
            print(f"{resume_key}: career {request.career_months} months")
            lines = request.messages[1]["content"].splitlines()
            alias_of_fact = {ref.id: alias for alias, ref in request.facts.items()}
            for n, row in enumerate(truth):
                prefix = f"[{alias_of_fact[row['id']]}] "
                print(f"  #{n:<3} {next(line for line in lines if line.startswith(prefix))}")
        print(f"-- {posting_id}: {postings[posting_id]['title'][:70]}")
        for requirement in requirements:
            years = f" {requirement['min_years']}y" if requirement["min_years"] else ""
            words = " ".join(requirement["text"].split())[:170]
            print(
                f"  {requirement['ordinal']:>2} [{requirement['importance'][:3]}/{requirement['kind']}{years}] {words}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 0 only if gold.jsonl is what the inputs build")
    parser.add_argument("--show", nargs="+", metavar="ID", help="print the sheet for a resume and then its postings")
    args = parser.parse_args(argv)
    if args.show:
        show(args.show[0], args.show[1:])
        return 0
    text = render(build(*load_inputs()))
    if args.check:
        current = GOLD.exists() and GOLD.read_text(encoding="utf-8") == text
        print("gold.jsonl is current" if current else "gold.jsonl is out of date: run build_gold.py", file=sys.stderr)
        return 0 if current else 1
    GOLD.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {GOLD.name}: {len(text.splitlines())} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
