"""Builds the tailoring-integrity gold set and its recordings from authoring.py.

    cd backend && uv run python ../evals/tailoring_integrity/build_gold.py            # write every generated file
    cd backend && uv run python ../evals/tailoring_integrity/build_gold.py --check    # exit 1 if one is out of date
    cd backend && uv run python ../evals/tailoring_integrity/build_gold.py --show     # the sheet a labeller works from

Generated: gold.jsonl (one pair per line, as the scorer reads it), recordings/<pair>.json (what the fake provider says
for that pair: the tailor.propose answer, then the verify.entail answer when the labels say layer 2 is reached),
recordings/manifest.json (which prompt version each recording was made for) and recordings/degraded_entail.json (the
damaged entailment answers the gate must fail on). The recordings are derived from the labels and from nothing else: the
propose answer is the labelled edits as a model would have written them, and verify.entail says "entailed" for exactly
the edits labelled supported. They prove wiring, not model quality (README.md).
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from app.tailor import pipeline, resume
from app.tailor.resume import Fact

HERE = Path(__file__).parent
GOLD = HERE / "gold.jsonl"
RECORDINGS = HERE / "recordings"
MODEL_KEYS = ("op", "target", "section", "after_text", "supporting", "requirements", "order")


def load_authoring() -> Any:
    spec = importlib.util.spec_from_file_location("tailoring_integrity_authoring", HERE / "authoring.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_pair(authoring: Any, source: dict) -> dict:
    """One gold item: the candidate's facts as profile.api.usable_facts rows, the requirements, and every proposed edit
    as the model's answer would carry it, plus its label."""
    candidate = source["candidate"]
    facts = authoring.CANDIDATES[candidate]
    ids = {f["tag"]: f"{candidate}-{f['tag']}" for f in facts}
    aliases = {f["tag"]: f"f{number}" for number, f in enumerate(facts, 1)}
    rows = [
        {
            "id": ids[f["tag"]],
            "kind": f["kind"],
            "statement": f["statement"],
            "attributes": f["attributes"],
            "parent_fact_id": ids[f["parent"]] if f["parent"] else None,
            "source": f["source"],
        }
        for f in facts
    ]
    by_tag = {f["tag"]: Fact(**row) for f, row in zip(facts, rows, strict=True)}
    kinds = {tag: fact.kind for tag, fact in by_tag.items()}
    edits = []
    for number, edit in enumerate(source["edits"], 1):
        first = edit["target"] or (edit["order"] or edit["support"] or [None])[0]
        after = edit["after"] if edit["after"] is not None else resume.item_text(by_tag[edit["support"][0]])
        edits.append(
            {
                "ordinal": number,
                "op": edit["op"],
                "target": aliases[edit["target"]] if edit["target"] else None,
                "section": pipeline.SECTION_OF_KIND.get(kinds.get(first, ""), "experience"),
                "after_text": after,
                "supporting": [aliases[tag] for tag in edit["support"]],
                "requirements": [f"r{n}" for n in edit["reqs"]],
                "order": [aliases[tag] for tag in edit["order"]],
                "label": edit["label"],
                "kind": edit["kind"],
                "reason": edit["reason"],
                "why": edit["why"],
            }
        )
    return {
        "key": source["key"],
        "labels": authoring.LABELS,
        "candidate": candidate,
        "allow_rephrase": source["allow_rephrase"],
        "facts": rows,
        "requirements": [{"id": n, **r} for n, r in enumerate(source["requirements"], 1)],
        "edits": edits,
    }


def build(authoring: Any) -> list[dict]:
    items = [build_pair(authoring, source) for source in authoring.PAIRS]
    assert len({item["key"] for item in items}) == len(items) == 30, "thirty pairs, each with its own key"
    for item in items:
        assert 3 <= len(item["edits"]) <= 6, item["key"]
        assert 1 <= len(item["requirements"]) <= 8, item["key"]
        if not item["allow_rephrase"]:
            assert all(e["op"] != "rephrase" for e in item["edits"]), item["key"]
    return items


def reaches_layer_2(edit: dict) -> bool:
    """Which edits the labels say layer 2 sees: rephrases that are supported, and rephrases only layer 2 can catch."""
    return edit["op"] == "rephrase" and (edit["label"] == "supported" or edit["reason"] == "not_entailed")


def recording(item: dict) -> list[Any]:
    """What the fake provider says for one pair, in the order the pipeline asks."""
    answer: list[Any] = [{"edits": [{key: edit[key] for key in MODEL_KEYS} for edit in item["edits"]]}]
    claims = [edit for edit in item["edits"] if reaches_layer_2(edit)]
    if claims:
        entailed = [{"edit": f"e{n}", "entailed": edit["label"] == "supported"} for n, edit in enumerate(claims, 1)]
        answer.append({"items": entailed})
    return answer


def degraded_entail(items: list[dict]) -> list[Any]:
    """One entry per pair, in pair order: null where the labels say layer 2 is not reached, else an answer that says
    every claim is entailed. Replaying it in place of the real entailment answers is the fault the gate must catch."""
    out: list[Any] = []
    for item in items:
        claims = [edit for edit in item["edits"] if reaches_layer_2(edit)]
        every_claim = {"items": [{"edit": f"e{n}", "entailed": True} for n in range(1, len(claims) + 1)]}
        out.append(every_claim if claims else None)
    return out


def manifest(items: list[dict]) -> dict[str, dict[str, str]]:
    """{file: {task: prompt_version}}: every task each recording answers, and the prompt it was made for."""
    entries: dict[str, dict[str, str]] = {}
    for item in items:
        tasks = {pipeline.PROPOSE_TASK: pipeline.PROPOSE_VERSION}
        if any(reaches_layer_2(edit) for edit in item["edits"]):
            tasks[pipeline.ENTAIL_TASK] = pipeline.ENTAIL_VERSION
        entries[f"{item['key']}.json"] = tasks
    entries["degraded_entail.json"] = {pipeline.ENTAIL_TASK: pipeline.ENTAIL_VERSION}
    return entries


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def render(items: list[dict]) -> dict[Path, str]:
    """Every generated file as text, by path."""
    files = {GOLD: "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)}
    for item in items:
        files[RECORDINGS / f"{item['key']}.json"] = dumps(recording(item))
    files[RECORDINGS / "degraded_entail.json"] = dumps(degraded_entail(items))
    files[RECORDINGS / "manifest.json"] = dumps(manifest(items))
    return files


def counts(items: list[dict]) -> dict[str, Any]:
    edits = [edit for item in items for edit in item["edits"]]
    kinds: dict[str, int] = {}
    for edit in edits:
        kinds[edit["kind"]] = kinds.get(edit["kind"], 0) + 1
    return {
        "pairs": len(items),
        "edits": len(edits),
        "supported": sum(e["label"] == "supported" for e in edits),
        "unsupported": sum(e["label"] == "unsupported" for e in edits),
        "kinds": dict(sorted(kinds.items())),
        "reasons": {r: sum(e["reason"] == r for e in edits) for r in sorted({e["reason"] for e in edits} - {None})},
        "ops": {op: sum(e["op"] == op for e in edits) for op in ("rephrase", "reorder", "remove", "add_from_fact")},
        "rephrase_off_pairs": sum(not item["allow_rephrase"] for item in items),
    }


def show(items: list[dict]) -> str:
    """The sheet a labeller works from: facts as the verifier sees them, then every edit with its label."""
    lines: list[str] = []
    for item in items:
        lines.append(f"== {item['key']} ({item['candidate']}, rephrase {'on' if item['allow_rephrase'] else 'off'})")
        for number, fact in enumerate(item["facts"], 1):
            marker = " (not on the resume)" if fact["source"] == "confirmed_proposal" else ""
            lines.append(f"  [f{number}] {fact['kind']}{marker}: {' '.join(fact['statement'].split())}")
        for requirement in item["requirements"]:
            lines.append(f"  [r{requirement['id']}] ({requirement['importance']}) {requirement['text']}")
        for edit in item["edits"]:
            what = edit["after_text"] or " ".join(edit["order"]) or edit["target"]
            lines.append(
                f"  #{edit['ordinal']} {edit['op']} {edit['target'] or ''} -> {what!r}"
                f" support {edit['supporting']}: {edit['label']} {edit['reason'] or ''} {edit['kind']}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a generated file is not what this builds")
    parser.add_argument("--show", action="store_true", help="print the labeller's sheet")
    args = parser.parse_args(argv)
    items = build(load_authoring())
    if args.show:
        print(show(items), end="")
        return 0
    files = render(items)
    if args.check:
        stale = [path.name for path, text in files.items() if not path.exists() or path.read_text("utf-8") != text]
        extra = sorted(p.name for p in RECORDINGS.glob("*.json") if p not in files) if RECORDINGS.exists() else []
        for name in (*stale, *extra):
            print(f"out of date: {name}", file=sys.stderr)
        return 1 if stale or extra else 0
    RECORDINGS.mkdir(exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    print(json.dumps(counts(items), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
