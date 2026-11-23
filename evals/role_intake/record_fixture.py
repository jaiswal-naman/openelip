"""Write recorded.jsonl and fixtures/degraded.jsonl: for every gold description, the answer a model would give if it
read the description exactly as labelled, and a damaged copy of those answers.

    uv run --project backend python evals/role_intake/record_fixture.py

These are fixtures built by code from the labels, not model output: each line is tagged source "labels_fixture", and a
replay of them (score.py) shows that the path from a recorded answer to scored rows works, not how well a model reads
employer text. Each line holds the answers the fake provider replays for one description, and a hash of the request they
answer, so a replay notices when the prompt or the segmenter has changed since (README). The answer cites the numbered
lines that hold each labelled item and quotes its words, the way the prompt asks a model to; building it checks that the
validator turns every answer back into exactly the labelled spans, so a label the validator cannot find (a quote that
occurs earlier in its lines, say) is reported here and not in a failed replay.

fixtures/degraded.jsonl is the same recording damaged on purpose, which the gate must fail: the importance of every
third item (counted over the whole set, from the first) is turned around, and one item that keeps its importance gets
words its description does not contain, so the validator must turn it away.

A recording from a live model needs a model key and is a manual check (README); nothing here calls one.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from app.ingest.api import segments, to_rows
from app.ingest.requirements.schema import RequirementList

HERE = Path(__file__).parent
DEGRADED = HERE / "fixtures" / "degraded.jsonl"


def load_scorer():
    """This suite's score.py, by a name of its own (several suites have one)."""
    spec = importlib.util.spec_from_file_location("role_intake_scorer_for_recorder", HERE / "score.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


suite = load_scorer()


def answer_for(row: dict[str, Any]) -> dict[str, Any]:
    """The RequirementList of a model that read the description as labelled: each item cites the numbered lines it
    overlaps and quotes its own words."""
    lines = segments(row["description"])
    items = []
    for item in row["requirements"]:
        cited = [line.n for line in lines if line.start < item["end"] and item["start"] < line.end]
        items.append(
            {
                "segments": cited,
                "quote": item["text"],
                "importance": item["importance"],
                "kind": item["kind"],
                "min_years": item["min_years"],
            }
        )
    return {"requirements": items}


def build() -> list[dict[str, Any]]:
    """One recording per gold description, in gold order."""
    recordings = []
    for row in suite.phase2.read_jsonl(suite.GOLD):
        answer = answer_for(row)
        result = to_rows(row["description"], segments(row["description"]), RequirementList.model_validate(answer))
        got = [(r.start, r.end, r.importance, r.kind, r.min_years) for r in result.rows]
        wanted = [(i["start"], i["end"], i["importance"], i["kind"], i["min_years"]) for i in row["requirements"]]
        if got != wanted or result.rejected:
            raise ValueError(f"{row['posting_id']}: the answer built from the labels does not read back as the labels")
        recordings.append(
            {
                "posting_id": row["posting_id"],
                "split": row["split"],
                "source": suite.FIXTURE,
                "request_sha256": suite.phase2.request_hash(row["description"]),
                "answers": [answer],
            }
        )
    return recordings


def degrade(recordings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A damaged copy: see the module docstring. The unverifiable item is the first one that is not turned around, in a
    description with another item to keep, so the validator still has rows and asks for no second answer."""
    damaged: list[dict[str, Any]] = json.loads(json.dumps(recordings))
    index, altered = 0, False
    for line in damaged:
        items = line["answers"][0]["requirements"]
        for item in items:
            if index % 3 == 0:
                item["importance"] = "preferred" if item["importance"] == "required" else "required"
            elif not altered and len(items) >= 2:
                item["quote"] += " (unverified)"  # words that are not in the description
                altered = True
            index += 1
    return damaged


def write(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8", newline="\n"
    )


def main() -> int:
    recordings = build()
    write(suite.RECORDED, recordings)
    write(DEGRADED, degrade(recordings))
    print(
        f"{suite.RECORDED.name}: {len(recordings)} descriptions, source {suite.FIXTURE}; wrote fixtures/{DEGRADED.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
