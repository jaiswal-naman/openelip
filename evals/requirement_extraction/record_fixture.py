"""Write recordings.jsonl: for every gold posting, the answer the heading rules give, in the fake provider's format.

    uv run --project backend python evals/requirement_extraction/record_fixture.py

These are fixtures built by code, not model output: each line is tagged source "rules_fixture", and a replay of them
(score.py --method model) shows that the path from a recorded answer to scored rows works, not how well a model
extracts. Each line holds the answers the fake provider replays for one posting, and a hash of the request they answer,
so a replay notices when the prompt or the segmenter has changed since (README). The answer cites the numbered line
that holds each rule row and quotes the row's text, the way the prompt asks a model to.

A recording from a live model needs a model key and is a manual check (README); nothing here calls one.
"""

import json
import sys
from typing import Any

from score import FIXTURE, GOLD, RECORDINGS, read_jsonl, request_hash

from app.ingest.api import rules_extract, segments


def answer_for(description: str) -> dict[str, Any]:
    """The RequirementList the heading rules amount to for this posting."""
    lines = segments(description)
    items = []
    for row in rules_extract(description):
        line = next(s for s in lines if s.start <= row.start and row.end <= s.end)  # every row sits inside one line
        items.append(
            {
                "segments": [line.n],
                "quote": row.text,
                "importance": row.importance,
                "kind": row.kind,
                "min_years": row.min_years,
            }
        )
    return {"requirements": items}


def build() -> list[dict[str, Any]]:
    """One recording per gold posting, dev and test, in gold order."""
    return [
        {
            "posting_id": posting["posting_id"],
            "split": posting["split"],
            "source": FIXTURE,
            "request_sha256": request_hash(posting["description"]),
            "answers": [answer_for(posting["description"])],
        }
        for posting in read_jsonl(GOLD)
    ]


def main() -> int:
    lines = build()
    RECORDINGS.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8", newline="\n"
    )
    print(f"{RECORDINGS.name}: {len(lines)} postings, source {FIXTURE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
