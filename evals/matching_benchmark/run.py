"""Run the matching benchmark and record it (README "Commands", plan 04-07; threats T-04-37, T-04-39).

    cd backend
    uv run python ../evals/matching_benchmark/run.py --database-url postgresql://app:...@127.0.0.1:5433/<name>_test

It acts on a benchmark database only (a name ending in _test, at the head of this checkout's migrations), makes no write
to the job index, the record or the match ledger, and calls no model for M1 to M4. M5 and M6 need a model key: with none
they are reported as "not run: needs a model key". It writes results.json (report.py turns that into REPORT.md) and
records the run as one eval_runs row, with one eval_items row per profile, in the benchmark database.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics  # noqa: E402
import target  # noqa: E402

PROFILES = HERE / "profiles.jsonl"
POOL = HERE / "pool.jsonl"
LABELS = HERE / "labels.jsonl"
META = HERE / "snapshot.meta.json"
RESULTS = HERE / "results.json"
MARKER = "agent-drafted, maintainer review pending"
NAMES = {
    "M1": "Keyword",
    "M2": "Vector",
    "M3": "Hybrid",
    "M4": "Hybrid plus rules",
    "M5": "Full cascade",
    "M6": "Score only what is shown",
}
RETRIEVAL = ("M1", "M2", "M3", "M4")  # no model call
QUALITY = ("ndcg_at_5", "precision_at_5", "recall_at_40", "recall_at_300")
NOT_RUN = "not run: needs a model key"
NO_REQUIREMENTS = "not run: needs the postings' requirements extracted first (a model-backed step)"
TOP_KEPT = 10  # jobs per list kept in results.json, the ones the pool was drawn from
IDLE_SECONDS = 0.6  # assumption: pause before each timed call, so CPU time is not the embedding runtime's idle spinning
MICRO = 1_000_000


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def timed(work: Callable[[], Any], *, settle: float | None = None) -> tuple[Any, float, float]:
    """(result, wall-clock seconds, process CPU seconds) of one call, after `settle` seconds of idleness (default
    IDLE_SECONDS): the embedding runtime's worker threads keep spinning for about half a second after a call (a probe
    measured 3.9 CPU seconds in 0.5 s idle), which would be counted as the CPU of whatever method runs next."""
    time.sleep(IDLE_SECONDS if settle is None else settle)
    wall, cpu = time.perf_counter(), time.process_time()
    result = work()
    return result, time.perf_counter() - wall, time.process_time() - cpu


class EmbeddingClock:
    """CPU seconds spent in the local embedding model, measured by wrapping what retrieval calls (AI_SYSTEM 12.5 reports
    local embedding CPU seconds beside the model cost)."""

    def __init__(self) -> None:
        from app.match import retrieval

        self.seconds = 0.0
        self._embed = retrieval.embed_passages

        def wrapped(texts: list[str], **options: Any) -> list[list[float]]:
            start = time.process_time()
            try:
                return self._embed(texts, **options)
            finally:
                self.seconds += time.process_time() - start

        retrieval.embed_passages = wrapped

    def warm(self) -> None:
        """Load the model before anything is timed."""
        self._embed(["warm up"])


def ledger_mark() -> int:
    from app.platform import db

    with db.transaction(role="worker") as conn:
        return int(conn.execute(text("SELECT coalesce(max(id), 0) FROM llm_calls")).scalar_one())


def ledger_since(mark: int) -> dict[str, int]:
    """The evidence-task calls the gateway logged after `mark`, from its own ledger (llm_calls)."""
    from app.platform import db

    with db.transaction(role="worker") as conn:
        row = conn.execute(
            text(
                """SELECT count(*) AS calls, coalesce(sum(input_tokens), 0) AS input_tokens,
                          coalesce(sum(output_tokens), 0) AS output_tokens, coalesce(sum(cost_microusd), 0) AS cost
                     FROM llm_calls WHERE id > :mark AND task = 'match.evidence.score'"""
            ),
            {"mark": mark},
        ).one()
    return {  # sums of integers come back as Decimal
        "calls": int(row.calls),
        "input_tokens": int(row.input_tokens),
        "output_tokens": int(row.output_tokens),
        "cost_microusd": int(row.cost),
    }


def scored_row(
    ranked: Sequence[str], labels: dict[str, int], violations: int, wall: float, cpu: float
) -> dict[str, Any]:
    return {
        **metrics.score_list(ranked, labels),
        "violations": violations,
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "top": list(ranked[:TOP_KEPT]),
    }


def run_profile(
    conn: Any,
    user_id: UUID,
    wanted: Sequence[str],
    refs: dict[int, str],
    labels: dict[str, int],
    now: datetime,
    clock: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Every wanted method over one profile: (rows by method, and why each model-backed method that could not run)."""
    import methods

    steps: dict[str, Callable[[], Any]] = {
        "M1": lambda: methods.keyword(conn, user_id),
        "M2": lambda: methods.vector(conn, user_id),
        "M3": lambda: methods.hybrid(conn, user_id),
        "M4": lambda: methods.hybrid_rules(conn, user_id, now),
    }
    rows: dict[str, dict[str, Any]] = {}
    lists: dict[str, list[Any]] = {}
    for name in RETRIEVAL:
        if name in wanted or (name == "M4" and {"M5", "M6"} & set(wanted)):
            before = clock.seconds
            lists[name], wall, cpu = timed(steps[name])
            rows[name] = scored_row(
                [refs[j.job_id] for j in lists[name]],
                labels,
                methods.violation_count(conn, user_id, lists[name]),
                wall,
                cpu,
            )
            rows[name]["embed_cpu_seconds"] = clock.seconds - before
            rows[name]["model"] = None
    rows = {name: row for name, row in rows.items() if name in wanted}
    not_run: dict[str, str] = {}
    for name in ("M5", "M6"):
        if name not in wanted:
            continue
        mark = ledger_mark()
        try:
            if name == "M5":
                (jobs, scoring, shown), wall, cpu = timed(
                    lambda: methods.full_cascade(conn, user_id, lists["M4"], now=now)
                )
            else:
                (jobs, scoring), wall, cpu = timed(lambda: methods.score_shown(conn, user_id, lists["M4"], now=now))
                shown = None
        except methods.ModelNotAvailable:
            not_run[name] = NOT_RUN
            continue
        except methods.RequirementsMissing:
            not_run[name] = NO_REQUIREMENTS
            continue
        row = scored_row(
            [refs[j.job_id] for j in jobs], labels, methods.violation_count(conn, user_id, jobs), wall, cpu
        )
        row["embed_cpu_seconds"] = 0.0
        row["model"] = ledger_since(mark)
        row |= {"scored": len(scoring.scored), "accepted": len(scoring.accepted), "rejected": scoring.rejected}
        row |= {"schema_invalid": scoring.schema_invalid, "shown_by_rules": shown}
        rows[name] = row
    return rows, not_run


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One method over a set of profiles: means with bootstrap intervals, the unjudged rate, violations, latency."""
    out: dict[str, Any] = {"profiles": len(rows)}
    for name in QUALITY:
        present = [r[name] for r in rows if r[name] is not None]
        out[name] = {
            "mean": metrics.mean(present),
            "profiles_with_a_value": len(present),
            "ci": list(metrics.bootstrap(present)) if present else None,
        }
    positions = sum(min(metrics.K, r["listed"]) for r in rows)
    out["unjudged_at_5_rate"] = sum(r["unjudged_at_5"] for r in rows) / positions if positions else None
    out["constraint_violations"] = sum(r["violations"] for r in rows)
    walls = [r["wall_seconds"] for r in rows]
    out["latency_p50_seconds"] = metrics.percentile(walls, 50)
    out["latency_p95_seconds"] = metrics.percentile(walls, 95)
    out["cpu_seconds_mean"] = sum(r["cpu_seconds"] for r in rows) / len(rows)
    out["embed_cpu_seconds_mean"] = sum(r["embed_cpu_seconds"] for r in rows) / len(rows)
    micro = sum((r["model"] or {}).get("cost_microusd", 0) for r in rows)
    out["cost_per_candidate_day_usd"] = micro / len(rows) / MICRO
    return out


def versus(method_rows: dict[str, dict[str, Any]], baseline_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The paired difference (method minus baseline) on each quality metric, with its bootstrap interval."""
    out: dict[str, Any] = {}
    for name in QUALITY:
        keys = sorted(method_rows)
        paired = metrics.paired_difference([method_rows[k][name] for k in keys], [baseline_rows[k][name] for k in keys])
        if paired is not None:
            (difference, (low, high)) = paired
            out[name] = {"mean_difference": difference, "ci": [low, high], "detected": not low <= 0 <= high}
    return out


def verdict(entry: dict[str, Any] | None, expected_sign: int) -> str:
    """supported, contradicted or undetermined, for a difference expected to be >= 0 (sign +1) or <= 0 (sign -1)."""
    if entry is None:
        return "undetermined"
    low, high = entry["ci"]
    if expected_sign > 0:
        return "supported" if low > 0 else "contradicted" if high < 0 else "undetermined"
    return "supported" if high < 0 else "contradicted" if low > 0 else "undetermined"


def expectations(by_method: dict[str, Any]) -> dict[str, str]:
    """README "Expectations written before the run": ndcg_at_5 of M1 and M2 not above M3's, M4's not below it."""

    def difference(method: str) -> dict[str, Any] | None:
        return by_method.get(method, {}).get("slices", {}).get("all", {}).get("versus_m3", {}).get("ndcg_at_5")

    return {
        "E1_m3_not_worse_than_m1": verdict(difference("M1"), -1),
        "E1_m3_not_worse_than_m2": verdict(difference("M2"), -1),
        "E2_m4_not_worse_than_m3": verdict(difference("M4"), +1),
    }


def aggregate(per_profile: dict[str, dict[str, dict[str, Any]]], has_constraints: dict[str, bool]) -> dict[str, Any]:
    """Everything results.json holds for the methods that ran: overall and by slice, and the differences from M3."""
    ran = [m for m in NAMES if all(m in per_profile[k] for k in per_profile)]
    slices = {
        "all": sorted(per_profile),
        "with_constraints": sorted(k for k in per_profile if has_constraints[k]),
        "without_constraints": sorted(k for k in per_profile if not has_constraints[k]),
    }
    out: dict[str, Any] = {}
    for m in ran:
        entry: dict[str, Any] = {"name": NAMES[m], "status": "run", "slices": {}}
        for label, keys in slices.items():
            entry["slices"][label] = summarize([per_profile[k][m] for k in keys])
            if m != "M3" and "M3" in ran:
                entry["slices"][label]["versus_m3"] = versus(
                    {k: per_profile[k][m] for k in keys}, {k: per_profile[k]["M3"] for k in keys}
                )
        out[m] = entry
    return out


def bars(per_profile: dict[str, dict[str, dict[str, Any]]], pool_labelled: bool) -> dict[str, bool]:
    rows = [(k, m, r) for k, by_method in per_profile.items() for m, r in by_method.items()]
    retrieval = [(k, m, r) for k, m, r in rows if m in RETRIEVAL]
    return {
        "constraint_violations_zero": all(r["violations"] == 0 for _, _, r in rows),
        "m1_m4_lists_complete": all(r["listed"] > 0 for _, _, r in retrieval),
        "m1_m4_unjudged_at_5_zero": all(r["unjudged_at_5"] == 0 for _, _, r in retrieval),
        "pool_labelled": pool_labelled,
    }


def record(
    results: dict[str, Any], per_profile: dict[str, dict[str, dict[str, Any]]], has_constraints: dict[str, bool]
) -> UUID:
    """The run as one eval_runs row of suite matching_benchmark and one eval_items row per profile (AI_SYSTEM 12.10).
    The schema's gate columns are left empty on purpose: the suite has no floor, the bars above are its own."""
    from app.evals.runner import git_sha
    from app.platform import db

    held = results["bars"]["score"] == 1.0
    summary = {
        "score": results["bars"]["score"],
        "report_only": True,
        "n": len(per_profile),
        "passed": sum(all(r["violations"] == 0 for r in by.values()) for by in per_profile.values()),
        "labels": MARKER,
        "methods": {
            m: {name: e["slices"]["all"][name]["mean"] for name in QUALITY} for m, e in results["methods"].items()
        },
    }
    with db.transaction(role="worker") as conn:
        run_id: UUID = conn.execute(
            text(
                "INSERT INTO eval_runs (suite, git_sha, config) "
                "VALUES (CAST('matching_benchmark' AS eval_suite), :sha, CAST(:config AS jsonb)) RETURNING id"
            ),
            {"sha": git_sha(), "config": json.dumps(results["run"]["config"])},
        ).scalar_one()
        for key, by_method in sorted(per_profile.items()):
            clean = all(r["violations"] == 0 for r in by_method.values())
            conn.execute(
                text(
                    "INSERT INTO eval_items (eval_run_id, item_key, slice, expected, actual, is_passed, score) "
                    "VALUES (:run, :key, :slice, CAST(:expected AS jsonb), CAST(:actual AS jsonb), :passed, :score)"
                ),
                {
                    "run": run_id,
                    "key": key,
                    "slice": "with_constraints" if has_constraints[key] else "without_constraints",
                    "expected": json.dumps({"constraint_violations": 0}),
                    "actual": json.dumps(
                        {
                            m: {n: r[n] for n in ("ndcg_at_5", "precision_at_5", "violations")}
                            for m, r in by_method.items()
                        }
                    ),
                    "passed": clean,
                    "score": 1.0 if clean else 0.0,
                },
            )
        conn.execute(
            text(
                "UPDATE eval_runs SET status = CAST(:status AS eval_status), metrics = CAST(:metrics AS jsonb), "
                "finished_at = now() WHERE id = :id"
            ),
            {"status": "passed" if held else "failed", "metrics": json.dumps(summary), "id": run_id},
        )
    return run_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", required=True, help="the benchmark database; its name must end in _test")
    ap.add_argument("--methods", default=",".join(NAMES), help="comma-separated, default all six")
    ap.add_argument("--no-record", action="store_true", help="print and write results.json without an eval_runs row")
    args = ap.parse_args()
    wanted = [m.strip() for m in args.methods.split(",")]
    url = target.require_scratch(args.database_url)
    target.point_app_at(url)
    from app.match.evidence import PROMPT_VERSION
    from app.match.ranking import RANKER_VERSION
    from app.platform import db
    from app.platform.llm.embed import active_model

    meta = json.loads(META.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(meta["exported_at"])
    profiles = {p["key"]: p for p in read_jsonl(PROFILES)}
    pool = read_jsonl(POOL)
    graded = read_jsonl(LABELS)
    labels: dict[str, dict[str, int]] = defaultdict(dict)
    for row in graded:
        labels[row["profile_key"]][row["job_ref"]] = row["grade"]
    pool_labelled = all(p["job_ref"] in labels[p["profile_key"]] for p in pool) and all(
        r["labels"] == MARKER for r in graded
    )
    with db.transaction(role="migrate") as admin:
        target.require_head(admin)
        refs, people = target.job_refs(admin), target.profile_ids(admin)
    assert sorted(people) == sorted(profiles), "the database does not hold the profiles of profiles.jsonl"
    clock = EmbeddingClock()
    clock.warm()
    per_profile: dict[str, dict[str, dict[str, Any]]] = {}
    not_run: dict[str, str] = {}
    for key in sorted(profiles):
        with db.transaction(user_id=people[key], role="match") as conn:
            per_profile[key], reasons = run_profile(conn, people[key], wanted, refs, labels[key], now, clock)
        not_run |= reasons
        print(f"{key}: " + ", ".join(f"{m} ndcg5 {r['ndcg_at_5']}" for m, r in per_profile[key].items()), flush=True)
    has_constraints = {k: bool(p["constraints"]) for k, p in profiles.items()}
    # A model-backed method that could not run for any profile is not part of the comparison.
    methods_run = [m for m in wanted if all(m in per_profile[k] for k in per_profile)]
    for key in per_profile:
        per_profile[key] = {m: per_profile[key][m] for m in methods_run}
    results: dict[str, Any] = {
        "run": {
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database": url.database,
            "label_status": MARKER,
            "snapshot": {"exported_at": meta["exported_at"], "postings": meta["selection"]["count"]},
            "profiles": len(profiles),
            "pairs": len(pool),
            "grades": {str(g): sum(r["grade"] == g for r in graded) for g in range(4)},
            "config": {
                "mode": "benchmark",
                "methods_run": methods_run,
                "ranker_version": RANKER_VERSION,
                "embedding_model": active_model(),
                "evidence_prompt_version": PROMPT_VERSION,
                "snapshot_exported_at": meta["exported_at"],
                "labels": MARKER,
                "report_only": True,
            },
        },
        "methods": aggregate(per_profile, has_constraints),
        "not_run": {m: not_run.get(m, NOT_RUN) for m in wanted if m not in methods_run},
        "profiles": {
            k: {"headline": profiles[k]["headline"], "hard_constraints": has_constraints[k], **per_profile[k]}
            for k in sorted(per_profile)
        },
    }
    held = bars(per_profile, pool_labelled)
    results["bars"] = {**held, "score": 1.0 if all(held.values()) else 0.0}
    results["expectations"] = expectations(results["methods"])
    if "M5" in methods_run and "M6" in methods_run:
        m5, m6 = ({k: per_profile[k][m] for k in per_profile} for m in ("M5", "M6"))
        results["m5_versus_m6"] = versus(m5, m6).get("ndcg_at_5")
    results["run"]["eval_run_id"] = None if args.no_record else str(record(results, per_profile, has_constraints))
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"bars {held}; score {results['bars']['score']}; run {results['run']['eval_run_id']}")
    return 0 if results["bars"]["score"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
