"""Write REPORT.md from results.json (README "Commands"). The report holds no number that is not in the recorded run:
render() only formats what run.py wrote, so a test can rebuild the report from the file and compare.

    cd backend
    uv run python ../evals/matching_benchmark/report.py
"""

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.json"
REPORT = HERE / "REPORT.md"
ORDER = ("M1", "M2", "M3", "M4", "M5", "M6")
QUALITY = ("ndcg_at_5", "precision_at_5", "recall_at_40", "recall_at_300")
SLICES = ("with_constraints", "without_constraints")
MARKER = "agent-drafted, maintainer review pending"


def f3(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def interval(ci: list[float] | None) -> str:
    return "n/a" if ci is None else f"{ci[0]:.3f} to {ci[1]:.3f}"


def reading(entry: dict[str, Any]) -> str:
    """What a paired difference says: only an interval that excludes 0 is a detected difference."""
    if not entry["detected"]:
        return "no detectable difference"
    return "detected: higher than M3" if entry["mean_difference"] > 0 else "detected: lower than M3"


def headline(results: dict[str, Any]) -> list[str]:
    lines = [
        "| Method | ndcg_at_5 (95% interval) | precision_at_5 | recall_at_40 | recall_at_300 | constraint_violations "
        "| unjudged at 5 | cost per candidate-day | latency p50 / p95 (s) | CPU s (embedding CPU s) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in ORDER:
        if m in results["methods"]:
            e = results["methods"][m]
            a = e["slices"]["all"]
            ndcg, p5, r40, r300 = (a[name] for name in QUALITY)
            lines.append(
                f"| {m} {e['name']} | {f3(ndcg['mean'])} ({interval(ndcg['ci'])}) | {f3(p5['mean'])} "
                f"| {f3(r40['mean'])} | {f3(r300['mean'])} | {a['constraint_violations']} "
                f"| {a['unjudged_at_5_rate']:.1%} | ${a['cost_per_candidate_day_usd']:.6f} "
                f"| {a['latency_p50_seconds']:.3f} / {a['latency_p95_seconds']:.3f} "
                f"| {a['cpu_seconds_mean']:.3f} ({a['embed_cpu_seconds_mean']:.3f}) |"
            )
        else:
            lines.append(f"| {m} | {results['not_run'][m]} | | | | | | | | |")
    return lines


def differences(results: dict[str, Any]) -> list[str]:
    lines = [
        "| Method | Metric | Mean difference from M3 | 95% interval | Reading |",
        "|---|---|---|---|---|",
    ]
    for m in ORDER:
        versus = results["methods"].get(m, {}).get("slices", {}).get("all", {}).get("versus_m3", {})
        for name in QUALITY:
            if name in versus:
                v = versus[name]
                lines.append(f"| {m} | {name} | {v['mean_difference']:+.3f} | {interval(v['ci'])} | {reading(v)} |")
    return lines


def slices(results: dict[str, Any]) -> list[str]:
    sizes = {s: results["methods"]["M3"]["slices"][s]["profiles"] for s in SLICES}
    lines = [
        "| Method | Slice | Profiles | ndcg_at_5 | precision_at_5 | ndcg_at_5 difference from M3 (95% interval) |",
        "|---|---|---|---|---|---|",
    ]
    for m in ORDER:
        if m not in results["methods"]:
            continue
        for s in SLICES:
            a = results["methods"][m]["slices"][s]
            versus = a.get("versus_m3", {}).get("ndcg_at_5")
            diff = "(baseline)" if m == "M3" else f"{versus['mean_difference']:+.3f} ({interval(versus['ci'])})"
            lines.append(
                f"| {m} | {s.replace('_', ' ')} | {sizes[s]} | {f3(a['ndcg_at_5']['mean'])} "
                f"| {f3(a['precision_at_5']['mean'])} | {diff} |"
            )
    return lines


def per_profile(results: dict[str, Any]) -> list[str]:
    ran = [m for m in ORDER if m in results["methods"]]
    lines = [
        "| Profile | Hard constraints | " + " | ".join(f"{m} ndcg_at_5" for m in ran) + " |",
        "|---|---|" + "---|" * len(ran),
    ]
    for key, row in results["profiles"].items():
        lines.append(
            f"| {key} {row['headline']} | {'yes' if row['hard_constraints'] else 'no'} | "
            + " | ".join(f3(row[m]["ndcg_at_5"]) for m in ran)
            + " |"
        )
    return lines


def render(results: dict[str, Any]) -> str:
    run, bars = results["run"], results["bars"]
    grades = run["grades"]
    expectation = {
        "E1_m3_not_worse_than_m1": "E1: M3 not worse than M1 on ndcg_at_5",
        "E1_m3_not_worse_than_m2": "E1: M3 not worse than M2 on ndcg_at_5",
        "E2_m4_not_worse_than_m3": "E2: M4 not worse than M3 on ndcg_at_5",
    }
    bar_names = {
        "constraint_violations_zero": "1. constraint_violations is 0 for every method that ran",
        "m1_m4_lists_complete": "2. M1 to M4 each produce a list for every profile",
        "m1_m4_unjudged_at_5_zero": "3. unjudged_at_5 is 0 for M1 to M4",
        "pool_labelled": "4. every pooled pair has a label, each marked agent-drafted",
    }
    not_run = results["not_run"]
    out = [
        "# Matching benchmark report",
        "",
        f"**Labels: {MARKER}.** One agent wrote the {run['profiles']} invented profiles and graded every pooled pair; "
        "nobody has reviewed the labels and no second rater has graded a sample. The profiles and labels are small "
        "(the target is 50 profiles and 1,000 to 2,000 pairs), so every interval below is wide. These numbers describe "
        "this snapshot, these profiles and these labels, on one machine, and nothing else (AI_SYSTEM 12.1, "
        "invariant 8). Read `README.md` for the pre-registered metrics and bars, and its limits.",
        "",
        f"Recorded run: {run['date']}; eval_runs row `{run['eval_run_id']}` in the scratch database "
        f"`{run['database']}` (dropped after the run, so the row is not kept: the numbers here and in `results.json` "
        "are the record). "
        f"{run['snapshot']['postings']} public postings exported {run['snapshot']['exported_at']}; {run['profiles']} "
        f"invented profiles; {run['pairs']} pooled pairs graded 0 to 3 (grade 0: {grades['0']}, 1: {grades['1']}, "
        f"2: {grades['2']}, 3: {grades['3']}). Ranker `{run['config']['ranker_version']}`, embedding model "
        f"`{run['config']['embedding_model']}`.",
        "",
        "## Headline",
        "",
        *headline(results),
        "",
        "ndcg_at_5, precision_at_5 and the recalls are means over profiles; recall is relative to the pooled, "
        "labelled jobs. Cost is the model dollars per candidate-day: exactly zero for the methods that call no model. "
        "CPU seconds are the process CPU seconds of one call, after an idle pause, with the part spent in the local "
        "embedding model in brackets (see the limits).",
        "",
        "## Differences from M3 (paired bootstrap)",
        "",
        "Profiles are resampled with replacement 2,000 times (seed 20261123); the interval is the 2.5th to 97.5th "
        "percentile of the resampled mean differences. A difference counts as detected only when its interval "
        "excludes 0.",
        "",
        *differences(results),
        "",
        "## Slices",
        "",
        *slices(results),
        "",
        "## Pre-registered bars",
        "",
        *(f"- {bar_names[name]}: {'holds' if bars[name] else 'FAILS'}" for name in bar_names),
        f"- the run's score is {bars['score']:.1f}. There is no floor in `evals/thresholds.toml`: this is a report, "
        "not a gate.",
        "",
        "## Expectations written before the run (not bars)",
        "",
        *(f"- {expectation[name]}: {verdict}" for name, verdict in results["expectations"].items()),
        "",
        "## M5 and M6",
        "",
        *(f"- {m}: {not_run[m]}." for m in ORDER if m in not_run),
        "- The pre-registered decision between M5 and M6 (M5 replaces M6 only if the lower bound of its ndcg_at_5 "
        "difference against M6 is above zero) waits for a live run: it needs a model key, and the postings' "
        "requirements extracted first.",
        "",
        "## Per profile",
        "",
        *per_profile(results),
        "",
        "## Limits",
        "",
        "- The labels are agent-drafted and unreviewed, by the agent that wrote the profiles.",
        "- The pool is the top 10 of M1 to M4, so a method outside the pool (M5 after a live run) can show unjudged "
        "positions, which count as not relevant.",
        "- Ingest stores no pay and no level, so salary-floor and level constraints filter nothing here, M4's level "
        "distance is 0, and every posting was first seen on the same day, so M4's recency term cannot reorder "
        "anything.",
        "- A card showed the first 600 characters of a posting, mostly company boilerplate, so most grades rest on the "
        "title.",
        "- Latency and CPU are from one machine with the posting vectors already stored. The embedding runtime's "
        "worker threads spin for about half a second after each call, so each timed call starts after a 0.6 s pause; "
        "CPU seconds are the whole process's during the call, the embedding's threads included.",
        "",
        "## Re-running",
        "",
        "```",
        "cd backend   # all four DATABASE_URL_* variables pointing at one scratch database ending in _test",
        "uv run python ../evals/matching_benchmark/load.py --database-url <that database>",
        "uv run python ../evals/matching_benchmark/run.py --database-url <that database>",
        "uv run python ../evals/matching_benchmark/report.py",
        "```",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    REPORT.write_text(render(results), encoding="utf-8", newline="\n")
    print(f"wrote {REPORT.name} from {RESULTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
