"""The location_accuracy suite (IDX-06): does parse_location read each posting's country and city, and leave unknown
what the text does not say?

    uv run --project backend python evals/location/score.py                  # the 300 test rows: print the numbers
    uv run --project backend python evals/location/score.py --split dev      # the 100 development rows, with misses
    uv run --project backend python evals/location/score.py --sample PATH    # another file of the same shape

    DATABASE_URL_WORKER=... uv run --project backend python evals/location/score.py --record \
        --report evals/location/baseline.json

--record writes the run through the eval harness's tables (eval_runs and one eval_items row per sample row, as the
worker role), so `python -m app.evals gate location_accuracy --run ID` can judge it, and --report writes that recorded
run as JSON. The bars are in README.md, written before the first run: location_accuracy >= 0.95 and
location_guessed == 0, on the test split. The labels are drafted by an agent and the maintainer's review is pending,
so every report says so.

The harness's `run` command replays model answers; this suite calls no model, so it is run from here and gated there.
"""

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import geonamescache

from app.ingest.api import parse_location

SUITE = "location_accuracy"
SAMPLE = Path(__file__).with_name("sample.jsonl")
MIN_ACCURACY = 0.95  # the pre-registered bars (README.md)
MAX_GUESSED = 0
LABELS = "agent-drafted, maintainer review pending"  # recorded in the run's config
REVIEW_NOTE = "labels drafted by an agent, review pending"  # printed beside every number
FIELDS = ("id", "split", "location_raw", "country_hint", "remote_hint", "label_country", "label_city")

Parser = Callable[[str | None, str | None, bool | None], tuple[str | None, str | None, bool | None]]


def load_sample(path: Path, split: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if missing := [f for f in FIELDS if f not in row]:
            raise ValueError(f"{path.name}: row {row.get('id')} lacks {', '.join(missing)}")
    return [row for row in rows if row["split"] == split]


def fold(text: str) -> str:
    """Lower case without accents or punctuation, so "Zürich" and "Zurich" are one spelling."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(ch for ch in plain if not unicodedata.combining(ch))).strip()


_CITY_IDS: dict[str, frozenset[int]] = {}  # every name looked up so far, so each name is searched for once


def city_ids(names: set[str]) -> dict[str, frozenset[int]]:
    """For each name, the GeoNames cities that carry it as their name or any alternate name. This is the scorer's own
    reading of GeoNames, apart from the parser's tables, so "Bangalore" and "Bengaluru" are the same city here."""
    new = {fold(name) for name in names} - _CITY_IDS.keys()
    if new:
        found: dict[str, set[int]] = defaultdict(set)
        for city in geonamescache.GeonamesCache().get_cities().values():
            for alias in (city["name"], *city["alternatenames"]):
                if alias and ord(alias[0]) <= 0x24F and (key := fold(alias)) in new:
                    found[key].add(city["geonameid"])
        _CITY_IDS.update({key: frozenset(found.get(key, ())) for key in new})
    return _CITY_IDS


def same_city(a: str, b: str, ids: dict[str, frozenset[int]]) -> bool:
    key_a, key_b = fold(a), fold(b)
    return key_a == key_b or bool(ids.get(key_a, frozenset()) & ids.get(key_b, frozenset()))


def judge(
    row: dict[str, Any], country: str | None, city: str | None, ids: dict[str, frozenset[int]]
) -> tuple[bool, bool]:
    """(correct, guessed). Null equals null only; a value where the label is null is a guess and is wrong."""
    label_city = row["label_city"]
    if label_city is None or city is None:
        city_ok = label_city is None and city is None
    else:
        city_ok = same_city(city, label_city, ids)
    guessed = (row["label_country"] is None and country is not None) or (label_city is None and city is not None)
    return country == row["label_country"] and city_ok, guessed


def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson score interval for correct out of n (AI_SYSTEM 12.1)."""
    if n == 0:
        return 0.0, 1.0
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def shape(row: dict[str, Any]) -> str:
    """What the label says, as the slice of the recorded item: unknown, country, city or country_and_city."""
    return {
        (False, False): "unknown",
        (True, False): "country",
        (False, True): "city",
        (True, True): "country_and_city",
    }[(row["label_country"] is not None, row["label_city"] is not None)]


def score(rows: list[dict[str, Any]], parse: Parser = parse_location) -> dict[str, Any]:
    """Run the parser over the rows. Returns the metrics and one item per row."""
    predictions = [parse(r["location_raw"], r["country_hint"], r["remote_hint"])[:2] for r in rows]
    names = {r["label_city"] for r in rows if r["label_city"]} | {city for _, city in predictions if city}
    ids = city_ids(names)
    items = []
    for row, (country, city) in zip(rows, predictions, strict=True):
        correct, guessed = judge(row, country, city, ids)
        items.append(
            {
                "key": row["id"],
                "slice": shape(row),
                "expected": {"country": row["label_country"], "city": row["label_city"]},
                "actual": {"country": country, "city": city},
                "is_passed": correct,
                "guessed": guessed,
            }
        )
    n, correct_n, guessed_n = len(items), sum(i["is_passed"] for i in items), sum(i["guessed"] for i in items)
    accuracy = correct_n / n if n else 0.0
    low, high = wilson(correct_n, n)
    bars_met = n > 0 and accuracy >= MIN_ACCURACY and guessed_n <= MAX_GUESSED
    metrics = {
        "n": n,
        "correct": correct_n,
        "location_accuracy": round(accuracy, 4),
        "location_guessed": guessed_n,
        "wilson_low": round(low, 4),
        "wilson_high": round(high, 4),
        "bars_met": bars_met,
        # How many rows could expose a guess: a label that is null is a place the text does not state.
        "null_labels": {
            "country": sum(r["label_country"] is None for r in rows),
            "city": sum(r["label_city"] is None for r in rows),
            "both": sum(shape(r) == "unknown" for r in rows),
        },
        # The harness gate reads these three. A guessed row is an absolute failure: it zeroes the gate's score.
        "passed": correct_n,
        "score": accuracy if guessed_n <= MAX_GUESSED else 0.0,
    }
    return {"metrics": metrics, "items": items}


def parser_sha256() -> str:
    """The parser file's content hash, line endings normalised so a Windows and a Linux checkout agree."""
    source = Path(inspect.getfile(parse_location)).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(source).hexdigest()


def run_config(split: str, sample: Path) -> dict[str, Any]:
    return {
        "mode": "code",  # no model is called
        "split": split,
        "sample": sample.name,
        "parser_sha256": parser_sha256(),
        "gazetteer": f"geonamescache {geonamescache.__version__}, GeoNames cities of 15,000 people or more",
        "labels": LABELS,
        "bars": {"location_accuracy": MIN_ACCURACY, "location_guessed": MAX_GUESSED},
    }


def record(result: dict[str, Any], config: dict[str, Any]) -> str:
    """Write the run as the harness does (eval_runs, one eval_items row per row, then the frozen verdict) and return
    its id. The database is the one DATABASE_URL_WORKER names."""
    from sqlalchemy import text

    from app.evals.runner import git_sha
    from app.platform import db

    metrics = result["metrics"]
    with db.transaction(role="worker") as conn:
        run_id = conn.execute(
            text(
                "INSERT INTO eval_runs (suite, git_sha, config) "
                "VALUES (CAST(:suite AS eval_suite), :sha, CAST(:config AS jsonb)) RETURNING id"
            ),
            {"suite": SUITE, "sha": git_sha(), "config": json.dumps(config)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO eval_items (eval_run_id, item_key, slice, expected, actual, is_passed, score) "
                "VALUES (:run, :key, :slice, CAST(:expected AS jsonb), CAST(:actual AS jsonb), :passed, :score)"
            ),
            [
                {
                    "run": run_id,
                    "key": item["key"],
                    "slice": item["slice"],
                    "expected": json.dumps(item["expected"]),
                    "actual": json.dumps(item["actual"]),
                    "passed": item["is_passed"],
                    "score": 1.0 if item["is_passed"] else 0.0,
                }
                for item in result["items"]
            ],
        )
        conn.execute(
            text(
                "UPDATE eval_runs SET status = CAST(:status AS eval_status), metrics = CAST(:metrics AS jsonb), "
                "is_gate_passed = :gate, finished_at = now() WHERE id = :id"
            ),
            {
                "status": "passed" if metrics["bars_met"] else "failed",
                "metrics": json.dumps(metrics),
                "gate": metrics["bars_met"],
                "id": run_id,
            },
        )
    return str(run_id)


def recorded_report(run_id: str) -> dict[str, Any]:
    """The run as the database holds it: what baseline.json publishes, so no number is typed in."""
    from sqlalchemy import text

    from app.platform import db

    with db.transaction(role="worker") as conn:
        run = (
            conn.execute(
                text(
                    "SELECT id::text AS run_id, suite::text AS suite, git_sha, config, status::text AS status, "
                    "is_gate_passed, started_at, finished_at, metrics FROM eval_runs WHERE id = CAST(:id AS uuid)"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )
        wrong = [
            row.item_key
            for row in conn.execute(
                text(
                    "SELECT item_key FROM eval_items WHERE eval_run_id = CAST(:id AS uuid) AND NOT is_passed "
                    "ORDER BY id"
                ),
                {"id": run_id},
            )
        ]
    return {
        **{key: run[key] for key in ("run_id", "suite", "status", "is_gate_passed", "git_sha")},
        "finished_at": run["finished_at"].isoformat(),
        "labels": REVIEW_NOTE,
        "config": run["config"],
        "metrics": run["metrics"],
        "wrong_items": wrong,
    }


def describe(split: str, metrics: dict[str, Any]) -> str:
    nulls = metrics["null_labels"]
    return (
        f"{SUITE} on the {split} split ({REVIEW_NOTE})\n"
        f"  {metrics['correct']} of {metrics['n']} rows correct: location_accuracy {metrics['location_accuracy']:.4f}, "
        f"95% Wilson interval {metrics['wilson_low']:.4f} to {metrics['wilson_high']:.4f}\n"
        f"  location_guessed {metrics['location_guessed']} (rows with a null label: "
        f"{nulls['country']} with no country, {nulls['city']} with no city, {nulls['both']} with neither)\n"
        f"  bars (location_accuracy >= {MIN_ACCURACY}, location_guessed == {MAX_GUESSED}): "
        f"{'MET' if metrics['bars_met'] else 'NOT MET'}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", choices=("dev", "test"), default="test")
    parser.add_argument("--sample", type=Path, default=SAMPLE, help="rows of the same shape as sample.jsonl")
    parser.add_argument("--record", action="store_true", help="write the run to the database DATABASE_URL_WORKER names")
    parser.add_argument("--report", type=Path, help="write the recorded run here as JSON (needs --record)")
    args = parser.parse_args(argv)
    if args.report and not args.record:
        parser.error("--report publishes a recorded run: add --record")
    if args.record and not os.environ.get("DATABASE_URL_WORKER"):
        parser.error("--record writes to the database DATABASE_URL_WORKER names; set it (its default is the live one)")
    result = score(load_sample(args.sample, args.split))
    print(describe(args.split, result["metrics"]))
    wrong = [i for i in result["items"] if not i["is_passed"]]
    for item in wrong:
        # The test rows are never shown, only named: a miss is reported, not tuned against.
        detail = f" {item['expected']} -> {item['actual']}" if args.split == "dev" else ""
        print(f"  wrong: {item['key']}{detail}{' (guessed)' if item['guessed'] else ''}")
    if args.record:
        run_id = record(result, run_config(args.split, args.sample))
        print(f"  recorded as run {run_id}")
        if args.report:
            report = recorded_report(run_id)
            args.report.write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"  report written to {args.report}")
    return 0 if result["metrics"]["bars_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
