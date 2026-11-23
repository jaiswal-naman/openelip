"""Builds gold.jsonl for the role_intake suite from cases.py, and checks it.

    uv run --project backend python evals/role_intake/build_gold.py            # write gold.jsonl
    uv run --project backend python evals/role_intake/build_gold.py --check    # exit 0 only if gold.jsonl is current

cases.py holds the 30 invented descriptions and the hand-drafted labels (quotes). The spans are found by Phase 2's
resolve.py, called here and not copied: it looks each quote up in its text exactly as written, and reports a quote that
is missing, ambiguous or overlapping another one. Spans are Python string indices, that is Unicode code points, the unit
PostgreSQL substr uses. Nothing else is read, no model is called, no database is touched.

A gold row has the shape of evals/requirement_extraction/gold.jsonl, `{posting_id, split, title, description,
requirements: [{start, end, text, importance, kind, min_years}]}`, plus the organization it is invented for, its
`slice` (the style of the text) and the marker `labels`. The set has no dev split: nothing is tuned on it (the prompt is
the one that reads postings), so every row is `test`.
"""

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).parent
EVALS = HERE.parent
GOLD = HERE / "gold.jsonl"
LABELS = "agent-drafted, maintainer review pending"
SPLIT = "test"
SIZE = 30
SLICES = {"prose", "bullets", "mixed", "short", "long", "non_ascii", "injection", "years"}


def load_script(name: str, path: Path) -> ModuleType:
    """A script of this repository by a name of its own: several suites have a score.py, a build_sample.py and a cases.py,
    so none of them is imported by its plain name."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def phase2_resolver() -> ModuleType:
    """Phase 2's resolve.py. It imports its sibling build_sample by plain name, so its folder is on the path while it
    loads."""
    folder = EVALS / "requirement_extraction"
    sys.path.insert(0, str(folder))
    try:
        return load_script("evals_requirement_extraction_resolve", folder / "resolve.py")
    finally:
        sys.path.remove(str(folder))


def label_of(case_id: str, label: tuple[Any, ...]) -> dict[str, Any]:
    quote, importance, kind, min_years, *occurrence = label
    item = {"quote": quote, "importance": importance, "kind": kind, "min_years": min_years}
    if occurrence:
        item["occurrence"] = occurrence[0]  # which match of a quote that appears more than once
    return item


def check_set(cases: list[dict[str, Any]], resolve: ModuleType) -> list[str]:
    problems = []
    ids = [case["id"] for case in cases]
    if len(cases) != SIZE or len(set(ids)) != len(ids):
        problems.append(f"{len(cases)} cases with {len(set(ids))} distinct ids, expected {SIZE}")
    for case in cases:
        where = case["id"]
        if not case["organization"].endswith("Demo"):
            problems.append(f"{where}: an invented organization's name ends in 'Demo'")
        if case["slice"] not in SLICES:
            problems.append(f"{where}: slice {case['slice']!r} is not one of {sorted(SLICES)}")
        if not case["description"].strip() or case["description"] != case["description"].rstrip("\n"):
            problems.append(f"{where}: the description is blank or ends in a newline")
        if resolve.has_contact_data(case["title"], case["description"]):
            problems.append(f"{where}: an email address or phone number (invented data carries none)")
    return problems


def build() -> tuple[list[dict[str, Any]], list[str]]:
    """The gold rows, and every problem found (nothing is written while there is one)."""
    resolve = phase2_resolver()
    cases = load_script("role_intake_cases", HERE / "cases.py").CASES
    rows: list[dict[str, Any]] = []
    problems = check_set(cases, resolve)
    for case in cases:
        posting = {"id": case["id"], "split": SPLIT, "title": case["title"], "description": case["description"]}
        label = {"posting_id": case["id"], "requirements": [label_of(case["id"], item) for item in case["labels"]]}
        row, found = resolve.gold_row(posting, label)
        problems += found
        rows.append({**row, "organization": case["organization"], "slice": case["slice"], "labels": LABELS})
    return rows, problems


def render(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def summary(rows: list[dict[str, Any]]) -> str:
    items = [item for row in rows for item in row["requirements"]]
    empty = sum(not row["requirements"] for row in rows)
    return (
        f"{len(rows)} descriptions ({empty} with none), {len(items)} items: "
        f"{dict(Counter(i['importance'] for i in items))}, {dict(Counter(i['kind'] for i in items))}, "
        f"slices {dict(Counter(r['slice'] for r in rows))}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with the committed gold.jsonl instead of writing it")
    args = parser.parse_args(argv)
    rows, problems = build()
    if args.check and not problems and (not GOLD.exists() or GOLD.read_text(encoding="utf-8") != render(rows)):
        problems.append("gold.jsonl is missing or is not what cases.py now resolves to; run build_gold.py")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    if not args.check:
        GOLD.write_text(render(rows), encoding="utf-8", newline="\n")
    print(("ok: " if args.check else "wrote gold.jsonl: ") + summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
