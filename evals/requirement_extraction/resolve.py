"""Turn the labelled quotes into character spans: postings.jsonl and labels.jsonl give gold.jsonl.

    uv run --project backend python evals/requirement_extraction/resolve.py          # write gold.jsonl
    uv run --project backend python evals/requirement_extraction/resolve.py --check  # compare with the committed file

A quote is looked up in the posting text exactly as written. It must match once, or `occurrence` (1 for the first)
says which match is meant. Spans are Python string indices, that is Unicode code points, the unit PostgreSQL substr
uses. Every problem is listed and the exit status is 1 when a quote is missing, ambiguous or overlaps another one;
nothing is written then.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from build_sample import DEV, MAX_CHARS, MIN_PER_ATS, SIZE, has_contact_data

HERE = Path(__file__).parent
POSTINGS, LABELS, GOLD = (HERE / name for name in ("postings.jsonl", "labels.jsonl", "gold.jsonl"))
POSTING_FIELDS = "id split ats board external_id title description".split()
IMPORTANCE = {"required", "preferred"}
KINDS = {"skill", "experience", "education", "certification", "authorization", "location", "other"}
ATS_NAMES = ("ashby", "greenhouse", "lever")
# min_years needs a number in the quote: digits, or a few number words ("at least five years").
NUMBER = re.compile(r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty)\b", re.I)


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def positions(text: str, quote: str) -> list[int]:
    found, i = [], text.find(quote)
    while i != -1:
        found.append(i)
        i = text.find(quote, i + 1)
    return found


def span_start(where: str, text: str, item: dict) -> tuple[int | None, str | None]:
    """The start index of the quote in `text`, or (None, what is wrong)."""
    quote, occurrence = item["quote"], item.get("occurrence")
    starts = positions(text, quote) if quote else []
    if not starts:
        return None, f"{where}: quote not found: {quote[:70]!r}"
    if occurrence is None:
        if len(starts) > 1:
            return None, f"{where}: quote appears {len(starts)} times, give occurrence: {quote[:70]!r}"
        return starts[0], None
    if len(starts) == 1 or not 1 <= occurrence <= len(starts):
        return None, f"{where}: occurrence {occurrence} but the quote appears {len(starts)} time(s)"
    return starts[occurrence - 1], None


def gold_row(posting: dict, label: dict) -> tuple[dict, list[str]]:
    pid, text = posting["id"], posting["description"]
    problems: list[str] = []
    items: list[dict] = []
    for n, item in enumerate(label["requirements"], 1):
        where = f"{pid} item {n}"
        years = item["min_years"]
        if item["importance"] not in IMPORTANCE or item["kind"] not in KINDS:
            problems.append(f"{where}: importance {item['importance']!r} or kind {item['kind']!r} is not allowed")
        if years is not None and not (isinstance(years, int) and years >= 1 and NUMBER.search(item["quote"])):
            problems.append(f"{where}: min_years {years!r} needs a positive whole number written in the quote")
        start, problem = span_start(where, text, item)
        if problem:
            problems.append(problem)
            continue
        end = start + len(item["quote"])
        items.append(
            {
                "start": start,
                "end": end,
                "text": text[start:end],
                "importance": item["importance"],
                "kind": item["kind"],
                "min_years": years,
            }
        )
    items.sort(key=lambda i: i["start"])
    problems += [f"{pid}: two items overlap at {b['start']}" for a, b in zip(items, items[1:]) if b["start"] < a["end"]]
    row = {"posting_id": pid, "split": posting["split"], "title": posting["title"], "description": text}
    return row | {"requirements": items}, problems


def check_inputs(postings: list[dict], labels: list[dict]) -> list[str]:
    problems: list[str] = []
    splits = Counter(p["split"] for p in postings)
    if len(postings) != SIZE or splits != {"dev": DEV, "test": SIZE - DEV}:
        problems.append(f"{len(postings)} postings split {dict(splits)}, expected {SIZE} ({DEV} dev)")
    by_ats = Counter(p["ats"] for p in postings)
    if any(by_ats[a] < MIN_PER_ATS for a in ATS_NAMES):
        problems.append(f"fewer than {MIN_PER_ATS} postings for an ATS: {dict(by_ats)}")
    ids = [p["id"] for p in postings]
    if len(set(ids)) != len(ids) or len({(p["ats"], p["board"], p["external_id"]) for p in postings}) != len(ids):
        problems.append("duplicate posting")
    for p in postings:
        if list(p) != POSTING_FIELDS:
            problems.append(f"{p.get('id')}: fields are {list(p)}")
        elif has_contact_data(p["title"], p["description"]) or len(p["description"]) > MAX_CHARS:
            problems.append(f"{p['id']}: contact data or longer than {MAX_CHARS} characters")
    label_ids = [label["posting_id"] for label in labels]
    if sorted(label_ids) != sorted(ids):
        problems.append("labels.jsonl must hold exactly one row for every posting")
    problems += [f"{label['posting_id']}: not labelled" for label in labels if not label.get("labeller")]
    return problems


def summary(rows: list[dict]) -> str:
    items = [i for r in rows for i in r["requirements"]]
    kinds = Counter(i["kind"] for i in items)
    importance = Counter(i["importance"] for i in items)
    empty = sum(not r["requirements"] for r in rows)
    return f"{len(rows)} postings ({empty} with none), {len(items)} items: {dict(importance)}, {dict(kinds)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed gold.jsonl instead of writing it")
    args = ap.parse_args()
    postings, labels = read(POSTINGS), read(LABELS)
    problems = check_inputs(postings, labels)
    by_id = {label["posting_id"]: label for label in labels}
    rows = []
    for p in postings:
        if p["id"] in by_id:
            row, found = gold_row(p, by_id[p["id"]])
            rows.append(row)
            problems += found
    if args.check and not problems:
        if not GOLD.exists() or read(GOLD) != rows:
            problems.append("gold.jsonl is missing or is not what the labels now resolve to; run resolve.py")
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        return 1
    if not args.check:
        with GOLD.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(("ok: " if args.check else "wrote gold.jsonl: ") + summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
