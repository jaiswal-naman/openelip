"""EVL-02: the matching benchmark (plan 04-07): its frozen snapshot, invented profiles and agent-drafted labels are what
the README says they are, its metrics and bootstrap are computed as pre-registered, the methods return no constraint
violation, M5 and M6 work on a fixture with the fake provider and report "not run" without a key, and the committed
report is the committed results.

Everything is invented or public: the profiles are invented, the postings were public, and the fixtures here use the
factories' invented candidate and employer. The tests that need a database use the eval tests' private one (`engine`),
with the embedding model replaced by a fixed vector, so none loads a model and none reaches a provider.
"""

import dataclasses
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.evals import EVALS
from app.ingest import embedding
from app.match import checker, retrieval
from app.platform import config, db
from app.platform.llm import fake, registry
from app.profile import api as profile
from app.profile import protected
from tests.match import factories

BENCH = EVALS / "matching_benchmark"
sys.path.insert(0, str(BENCH))  # the scripts import each other by name, as they do when run
import cards as bench_cards  # noqa: E402
import export_snapshot  # noqa: E402
import load as bench_load  # noqa: E402
import methods as bench_methods  # noqa: E402
import metrics  # noqa: E402
import pool as bench_pool  # noqa: E402
import report  # noqa: E402
import run as bench_run  # noqa: E402
import target  # noqa: E402

while str(BENCH) in sys.path:  # the scripts are all imported; nothing of theirs is left on the path to shadow a module
    sys.path.remove(str(BENCH))

MARKER = "agent-drafted, maintainer review pending"
NOW = datetime(2026, 11, 23, 6, 26, tzinfo=timezone.utc)
UNIT = [1.0] + [0.0] * 383  # the embedding model, replaced by one fixed vector
REAL_SETTINGS = config.get  # the cached reader, which a fixture replaces and target.point_app_at() clears


def jsonl(name):
    lines = (BENCH / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


SNAPSHOT = jsonl("snapshot.jsonl")
PROFILES = jsonl("profiles.jsonl")
POOL = jsonl("pool.jsonl")
LABELS = jsonl("labels.jsonl")
META = json.loads((BENCH / "snapshot.meta.json").read_text(encoding="utf-8"))
RESULTS = json.loads((BENCH / "results.json").read_text(encoding="utf-8"))


# ---- the metrics: worked by hand -------------------------------------------------------------------------------------


def test_ndcg_at_5_of_a_hand_made_list_is_the_value_worked_out_by_hand():
    # DCG: 7 / log2(2) + 0 + 3 / log2(4) = 8.5. Ideal [3, 2, 0, 0, 0]: 7 + 3 / log2(3).
    got = metrics.ndcg_at_k([3, 0, 2, 0, 0], [3, 2, 0, 0, 0])

    assert got == pytest.approx(8.5 / (7 + 3 / math.log2(3)))
    assert metrics.ndcg_at_k([3, 2, 0, 0, 0], [3, 2, 0, 0, 0]) == pytest.approx(1.0)


def test_ndcg_is_not_defined_for_a_profile_with_nothing_labelled_one_or_more():
    assert metrics.ndcg_at_k([0, 0, 0], [0, 0, 0, 0]) is None


def test_precision_counts_grade_two_or_more_and_divides_by_five_even_for_a_short_list():
    assert metrics.precision_at_k([3, 2, 1, 0, 0]) == pytest.approx(0.4)
    assert metrics.precision_at_k([3, 3]) == pytest.approx(0.4)  # two jobs listed, still over five


def test_recall_is_relative_to_the_relevant_labelled_jobs_and_none_without_any():
    assert metrics.recall_at_k([0, 3, 2, 0], relevant_total=4, k=40) == pytest.approx(0.5)
    assert metrics.recall_at_k([0, 3, 2, 0], relevant_total=4, k=2) == pytest.approx(0.25)
    assert metrics.recall_at_k([1, 1], relevant_total=0, k=40) is None


def test_a_job_that_was_never_labelled_counts_as_grade_zero_and_raises_the_unjudged_count():
    labels = {"a": 3, "b": 2}

    got = metrics.score_list(["a", "x", "b", "y", "z", "q"], labels)

    assert got["unjudged_at_5"] == 3  # x, y, z in the first five; q is past the cut-off
    assert got["ndcg_at_5"] == pytest.approx(metrics.ndcg_at_k([3, 0, 2, 0, 0], [3, 2]))
    assert got["precision_at_5"] == pytest.approx(0.4)
    assert got["listed"] == 6


def test_the_bootstrap_with_its_fixed_seed_returns_the_same_interval_on_two_calls():
    values = [0.1, -0.2, 0.3, 0.0, 0.25, -0.05, 0.4, 0.15]

    assert metrics.bootstrap(values) == metrics.bootstrap(values)
    assert metrics.bootstrap(values, seed=1) != metrics.bootstrap(values, seed=2)


def test_the_bootstrap_interval_of_two_identical_methods_is_zero_and_contains_zero():
    same = [0.5, 0.7, 0.2, 0.9]

    difference, (low, high) = metrics.paired_difference(same, same)

    assert difference == 0 and low <= 0 <= high


def test_a_paired_difference_skips_profiles_where_either_method_has_no_value():
    got = metrics.paired_difference([1.0, None, 0.5], [0.5, 0.2, None])

    assert got is not None and got[0] == pytest.approx(0.5)
    assert metrics.paired_difference([None], [1.0]) is None


def test_percentile_is_nearest_rank():
    values = [5.0, 1.0, 3.0, 2.0, 4.0]

    assert metrics.percentile(values, 50) == 3.0
    assert metrics.percentile(values, 95) == 5.0
    assert metrics.percentile(list(map(float, range(1, 21))), 95) == 19.0


# ---- the runner's aggregation, the bars and the readings ------------------------------------------------------------


def row(ndcg, *, violations=0, unjudged=0, listed=10, cost=0, wall=0.05):
    return {
        "ndcg_at_5": ndcg,
        "precision_at_5": 0.4,
        "recall_at_40": 1.0,
        "recall_at_300": 1.0,
        "unjudged_at_5": unjudged,
        "listed": listed,
        "violations": violations,
        "wall_seconds": wall,
        "cpu_seconds": wall,
        "embed_cpu_seconds": 0.0,
        "model": {"cost_microusd": cost} if cost else None,
    }


def test_summarize_reports_means_violations_the_unjudged_rate_and_the_cost_per_candidate_day():
    rows = [row(0.5, unjudged=1, cost=2_000_000), row(0.7, violations=2), row(None, listed=3)]

    got = bench_run.summarize(rows)

    assert got["ndcg_at_5"]["mean"] == pytest.approx(0.6) and got["ndcg_at_5"]["profiles_with_a_value"] == 2
    assert got["constraint_violations"] == 2
    assert got["unjudged_at_5_rate"] == pytest.approx(1 / 13)  # one of 5 + 5 + 3 positions
    assert got["cost_per_candidate_day_usd"] == pytest.approx(2.0 / 3)
    assert got["latency_p50_seconds"] == 0.05


def test_the_bars_fail_on_a_violation_an_empty_list_an_unjudged_position_or_an_unlabelled_pool():
    clean = {"p01": {m: row(0.5) for m in bench_run.RETRIEVAL}}
    assert all(bench_run.bars(clean, True).values())
    assert not bench_run.bars(clean, False)["pool_labelled"]
    dirty = {"p01": {**clean["p01"], "M3": row(0.5, violations=1)}}
    assert not bench_run.bars(dirty, True)["constraint_violations_zero"]
    empty = {"p01": {**clean["p01"], "M2": row(0.5, listed=0)}}
    assert not bench_run.bars(empty, True)["m1_m4_lists_complete"]
    unjudged = {"p01": {**clean["p01"], "M4": row(0.5, unjudged=1)}}
    assert not bench_run.bars(unjudged, True)["m1_m4_unjudged_at_5_zero"]


def test_a_difference_is_detected_only_when_its_interval_excludes_zero_and_the_expectations_read_the_sign():
    keys = [f"p{n:02d}" for n in range(1, 11)]
    better = {k: row(0.8) for k in keys}
    baseline = {k: row(0.5) for k in keys}
    mixed = {k: row(0.5 + (0.3 if n % 2 else -0.3)) for n, k in enumerate(keys)}

    assert bench_run.versus(better, baseline)["ndcg_at_5"]["detected"]
    assert not bench_run.versus(mixed, baseline)["ndcg_at_5"]["detected"]
    assert bench_run.verdict({"ci": [0.1, 0.3]}, +1) == "supported"
    assert bench_run.verdict({"ci": [0.1, 0.3]}, -1) == "contradicted"
    assert bench_run.verdict({"ci": [-0.1, 0.3]}, +1) == "undetermined"
    assert bench_run.verdict(None, -1) == "undetermined"


def test_timed_reports_wall_and_cpu_seconds_after_the_pause_it_is_given():
    result, wall, cpu = bench_run.timed(lambda: sum(range(10_000)), settle=0)

    assert result == sum(range(10_000)) and wall >= 0 and cpu >= 0


# ---- the guard on which database a script may touch ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["app", "app_prod", "postgres", "app_test_old", ""])
def test_a_script_refuses_a_database_whose_name_does_not_end_in_test(name):
    with pytest.raises(SystemExit):
        target.require_scratch(f"postgresql://app:secret@127.0.0.1:5433/{name}")


def test_pointing_the_application_at_a_scratch_database_replaces_all_four_roles(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    url = target.require_scratch("postgresql://app:app_local_only@127.0.0.1:5433/app_bench_test")

    target.point_app_at(url)

    expected = {"MIGRATE": "app", "API": "app_api", "WORKER": "app_worker", "MATCH": "app_match"}
    for role, login in expected.items():
        point = make_url(os.environ[f"DATABASE_URL_{role}"])
        assert (point.username, point.database) == (login, "app_bench_test")
    REAL_SETTINGS.cache_clear()  # the cache must not keep what this test pointed it at


def test_the_exporter_refuses_a_source_below_the_phase_two_and_three_merge_and_accepts_the_merge_and_later():
    assert not export_snapshot.descends_from("0002_platform")
    assert not export_snapshot.descends_from("p2_0201_job_index")
    assert export_snapshot.descends_from(export_snapshot.MERGE_REVISION)
    assert export_snapshot.descends_from(target.head_revision())
    assert not export_snapshot.descends_from("not_a_revision")


def test_the_exporter_picks_one_posting_per_group_before_a_second_and_cuts_a_prefix_at_the_size_limit(monkeypatch):
    def posting(source, country, number):
        return {"ats": "lever", "token": source, "country_hint": country, "ref": f"{source}{country}{number}"}

    groups = [("a", "US")] * 3 + [("a", "DE")] * 2 + [("b", "US")] * 2
    pool = [(f"{n:03d}", posting(source, country, n)) for n, (source, country) in enumerate(groups)]

    ordered = export_snapshot.order(pool)

    assert [p["ref"] for p in ordered][:3] == ["aUS0", "aDE3", "bUS5"]  # the first of every (source, country) group
    assert len(ordered) == 7
    monkeypatch.setattr(export_snapshot, "MAX_BYTES", 2 * len(export_snapshot.dump([ordered[0]])) + 10)
    assert export_snapshot.fit(ordered) == ordered[:2]


def test_a_posting_with_an_email_address_or_a_phone_number_is_recognised():
    assert export_snapshot.has_contact_data("Write to jobs@example.org", None)
    assert export_snapshot.has_contact_data("Call +49 30 1234 5678 now")
    assert not export_snapshot.has_contact_data("Senior engineer, 5+ years of Python", "Berlin")


@pytest.mark.parametrize(
    "written", ["accommodations[at]coinbase.com", "jobs (at) example.org", "Jobs [AT] Example.org", "a.b+c[at]sub.example.co.uk"]
)
def test_an_address_written_with_at_in_brackets_is_recognised_like_one_with_an_at_sign(written):
    assert export_snapshot.has_contact_data(f"To ask for an accommodation, write to {written} before you apply.")


def test_words_around_at_that_are_not_an_address_are_left_alone():
    assert not export_snapshot.has_contact_data("Engineer (at least 5 years) based at [Berlin]", "Looking for a lead (at) Stripe")


# ---- the frozen snapshot -------------------------------------------------------------------------------------------


def test_the_snapshot_is_the_size_and_count_its_metadata_states_and_is_under_the_limit():
    sel = META["selection"]

    assert len(SNAPSHOT) == sel["count"] and sel["count"] <= sel["target_count"]
    assert (BENCH / "snapshot.jsonl").stat().st_size == sel["size_bytes"] <= sel["size_limit_bytes"]
    assert sel["pool"] == sel["open_listed_canonical"] - sel["dropped_for_contact_data"]
    assert META["source"]["database"].endswith("_test")  # a restored copy, never the live database
    assert export_snapshot.descends_from(META["source"]["alembic_revision"])


def test_every_posting_has_the_fields_the_store_path_needs_and_a_ref_that_is_derived_from_its_identity():
    fields = (
        "ref ats token company external_id title description location_raw country_hint remote_hint employment_type "
        "department apply_url posted_at changed_at"
    ).split()

    for p in SNAPSHOT:
        assert list(p) == fields
        assert p["ref"] == export_snapshot.ref_of(p["ats"], p["token"], p["external_id"])
        assert len(p["description"]) >= 40 and p["remote_hint"] in (True, False, None)
    assert len({p["ref"] for p in SNAPSHOT}) == len(SNAPSHOT)
    assert len({(p["ats"], p["token"], p["external_id"]) for p in SNAPSHOT}) == len(SNAPSHOT)


def test_no_posting_in_the_snapshot_holds_an_email_address_or_a_phone_number():
    for p in SNAPSHOT:
        texts = (p["title"], p["description"], p["location_raw"], p["department"], p["apply_url"], p["company"])
        assert not export_snapshot.has_contact_data(*texts), p["ref"]


def test_the_obfuscated_addresses_taken_out_of_the_snapshot_are_replaced_in_place_and_counted_in_its_metadata():
    redacted = META["redacted"]  # the export filter then caught "@" only; the postings were kept and the strings replaced

    held = sum(p[field].count(redacted["replacement"]) for p in SNAPSHOT for field in ("title", "description"))

    assert redacted["replacement"] == "[email removed]"
    assert held == redacted["strings"] > 0
    assert len(SNAPSHOT) == META["selection"]["count"]  # no posting was dropped for it


# ---- the invented profiles ----------------------------------------------------------------------------------------


def test_there_are_twenty_invented_profiles_whose_addresses_and_names_say_so():
    assert [p["key"] for p in PROFILES] == [f"p{n:02d}" for n in range(1, 21)]
    for n, p in enumerate(PROFILES, 1):
        assert p["name"] == f"Demo Candidate {n:02d}" and p["timezone"]
        assert p["headline"] and p["facts"] and p["preferences"]


def test_every_role_is_dated_and_every_employer_and_school_is_marked_as_invented():
    for p in PROFILES:
        roles = [f for f in p["facts"] if f["kind"] == "role"]
        assert roles, p["key"]
        for fact in roles:
            attributes = fact["attributes"]
            assert re.fullmatch(r"\d{4}-\d{2}", attributes["start_date"]), p["key"]
            assert (attributes["end_date"] is None) == attributes["is_current"], p["key"]
            assert attributes["employer"].endswith("Demo") or attributes["employer"].endswith("Demo University")


def test_no_statement_of_any_profile_states_a_protected_characteristic_or_a_contact_detail():
    for p in PROFILES:
        statements = [x["statement"] for x in (*p["facts"], *p["constraints"], *p["preferences"])]
        for statement in statements:
            assert protected.screen_typed(statement) is None, (p["key"], statement)
            assert not export_snapshot.has_contact_data(statement), (p["key"], statement)


def test_the_constraints_use_the_kinds_and_fields_the_record_has_and_the_mix_is_the_one_the_readme_states():
    fields = {
        "location": "country_codes",
        "work_authorization": "country_codes",
        "remote_policy": "arrangements",
        "employment_type": "employment_types",
        "seniority": "seniorities",
        "salary_floor": "salary_floor",
        "excluded_company": "company_names",
    }
    kinds = []
    for p in PROFILES:
        for c in p["constraints"]:
            assert c["kind"] in fields and c[fields[c["kind"]]], (p["key"], c["kind"])
            kinds.append(c["kind"])
    assert sum(bool(p["constraints"]) for p in PROFILES) == 12
    assert {k: kinds.count(k) for k in set(kinds)} == {
        "location": 8,
        "remote_policy": 2,
        "work_authorization": 1,
        "employment_type": 1,
        "seniority": 1,
        "salary_floor": 1,
        "excluded_company": 2,
    }


# ---- the pool and the labels ---------------------------------------------------------------------------------------


def test_every_pooled_pair_has_one_agent_drafted_grade_and_every_label_is_for_a_pooled_pair():
    pairs = [(p["profile_key"], p["job_ref"]) for p in POOL]
    graded = [(x["profile_key"], x["job_ref"]) for x in LABELS]

    assert len(set(pairs)) == len(pairs) == len(LABELS) == len(set(graded)) and set(pairs) == set(graded)
    assert {x["labels"] for x in LABELS} == {MARKER}
    assert {x["grade"] for x in LABELS} <= {0, 1, 2, 3}
    assert all(0 < len(x["note"]) <= 120 for x in LABELS)
    assert {p["profile_key"] for p in POOL} == {p["key"] for p in PROFILES}
    assert {p["job_ref"] for p in POOL} <= {p["ref"] for p in SNAPSHOT}


def test_every_profile_has_at_least_one_relevant_pair_so_that_no_profile_is_left_out_of_a_mean():
    for p in PROFILES:
        grades = [x["grade"] for x in LABELS if x["profile_key"] == p["key"]]
        assert any(g >= metrics.RELEVANT for g in grades), p["key"]


def test_a_pool_entry_names_only_methods_of_the_pool_and_the_pool_takes_ten_from_each():
    assert all(p["methods"] and set(p["methods"]) <= {"M1", "M2", "M3", "M4"} for p in POOL)
    assert bench_pool.TOP == 10


def test_the_cards_hide_the_method_and_their_order_is_fixed_whatever_order_the_refs_come_in():
    refs = [p["job_ref"] for p in POOL if p["profile_key"] == "p01"]

    assert bench_cards.hidden_order("p01", refs) == bench_cards.hidden_order("p01", list(reversed(refs)))
    assert bench_cards.hidden_order("p01", refs) != bench_cards.hidden_order("p02", refs)
    block = bench_cards.profile_block(PROFILES[0])
    assert "p01" in block and "hard constraints" in block and "M1" not in block


def test_the_readme_was_written_with_the_pre_registered_metrics_bars_and_rule_and_names_every_method():
    readme = (BENCH / "README.md").read_text(encoding="utf-8")

    for needle in ("Pre-registered bars and rules", "ndcg_at_5", "precision_at_5", "recall_at_300", "recall_at_40"):
        assert needle in readme
    for needle in ("constraint_violations", "20261123", "2,000 times", "M5 replaces M6", MARKER):
        assert needle in readme
    assert all(f"| M{n} " in readme for n in range(1, 7))
    assert MARKER in (BENCH / "GUIDELINE.md").read_text(encoding="utf-8")


# ---- a database to run the methods on -----------------------------------------------------------------------------

EMPTY = (
    "TRUNCATE users, job_sources, jobs, queue_jobs, llm_calls, model_registry, eval_runs, eval_items "
    "RESTART IDENTITY CASCADE"
)


def settings_for(evals_db, **extra):
    """Settings whose four database roles all name the eval tests' private database (the conftest's settings leave the
    match role at its default, which is the live database)."""
    urls = {
        f"DATABASE_URL_{role}": make_url(os.environ[f"DATABASE_URL_{role}"])
        .set(database=evals_db.database)
        .render_as_string(hide_password=False)
        for role in ("MIGRATE", "API", "WORKER", "MATCH")
    }
    return config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **urls, **extra})


@pytest.fixture
def world(engine, evals_db, monkeypatch):
    """The private database, empty, with the fake provider and a fixed embedding in place of the model."""
    settings = settings_for(evals_db)
    monkeypatch.setattr(config, "get", lambda: settings)
    monkeypatch.setattr(db, "_engines", {})
    monkeypatch.setattr(retrieval, "embed_passages", lambda texts: [UNIT for _ in texts])
    monkeypatch.setattr(embedding, "embed_passages", lambda texts: [UNIT for _ in texts])
    monkeypatch.setattr(bench_run, "IDLE_SECONDS", 0)
    with engine.begin() as c:
        c.execute(text(EMPTY))
    yield engine
    for application_engine in db._engines.values():
        application_engine.dispose()
    fake.replay_from(None)


def test_the_loader_puts_snapshot_postings_through_the_store_path_with_their_first_seen_time_and_embeds_them(world):
    with world.begin() as conn:
        seen = bench_load.load_postings(conn, SNAPSHOT[:5])
        embedded = embedding.embed_pending(conn)
        rows = conn.execute(text("SELECT external_id, materially_changed_at, status::text FROM jobs")).all()
        refs = set(target.job_refs(conn).values())

    assert seen == embedded == 5 and len(rows) == 5
    assert refs == {p["ref"] for p in SNAPSHOT[:5]}  # the ref is recomputed from the stored identity
    by_id = {p["external_id"]: p for p in SNAPSHOT[:5]}
    for external_id, changed, status in rows:
        assert changed == datetime.fromisoformat(by_id[external_id]["changed_at"]) and status == "open"


def test_the_loader_inserts_each_profile_as_a_confirmed_record_whose_constraints_the_filter_can_read(world):
    p01, p02 = PROFILES[0], PROFILES[1]
    with world.begin() as conn:
        ids = bench_load.load_profiles(conn, [p01, p02])
        facts = profile.usable_facts(conn, ids["p02"])
        constraints = profile.compiled_constraints(conn, ids["p02"])
        version = conn.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": ids["p02"]}).scalar_one()
        confirmed = conn.execute(text("SELECT record_confirmed_at FROM users WHERE id = :u"), {"u": ids["p01"]})
        found = target.profile_ids(conn)

    assert found == ids  # the key to user id map is the address: <key>@example.test
    assert [f["statement"] for f in facts] == [f["statement"] for f in p02["facts"]]  # the order of the file
    assert constraints["location"]["country_codes"] == ["US", "CA"]
    assert constraints["remote_policy"]["arrangements"] == ["remote", "hybrid"]
    assert version == len(p02["facts"]) + len(p02["constraints"]) + len(p02["preferences"])
    assert confirmed.scalar_one() is not None


def test_the_loader_refuses_a_database_that_already_holds_postings(world, evals_db, monkeypatch):
    with world.begin() as conn:
        bench_load.load_postings(conn, SNAPSHOT[:1])
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(config, "get", REAL_SETTINGS)  # main() points the application at the database it was given
    monkeypatch.setattr(sys, "argv", ["load.py", "--database-url", evals_db.render_as_string(hide_password=False)])

    with pytest.raises(SystemExit) as stopped:
        bench_load.main()

    assert "already holds postings" in str(stopped.value)
    REAL_SETTINGS.cache_clear()


def load_tiny_benchmark(world):
    """Sixty snapshot postings, profile p01 (no constraints) and p02 (the United States or Canada, remote or hybrid)."""
    with world.begin() as conn:
        bench_load.load_postings(conn, SNAPSHOT[:60])
        ids = bench_load.load_profiles(conn, [PROFILES[0], PROFILES[1]])
        embedding.embed_pending(conn)
    return ids


def test_every_retrieval_method_returns_zero_constraint_violations_and_the_filter_removed_something(world):
    ids = load_tiny_benchmark(world)
    builders = {
        "M1": bench_methods.keyword,
        "M2": bench_methods.vector,
        "M3": bench_methods.hybrid,
        "M4": lambda conn, user: bench_methods.hybrid_rules(conn, user, NOW),
    }
    with world.connect() as conn:
        refs = target.job_refs(conn)
        for name, build in builders.items():
            constrained = build(conn, ids["p02"])
            assert bench_methods.violation_count(conn, ids["p02"], constrained) == 0, name
            assert all(c.facts.country in ("US", "CA", None) for c in constrained), name
            assert all(c.job_id in refs for c in constrained), name
            if name in ("M2", "M3"):  # every eligible job is ranked by the one fixed vector
                assert 0 < len(constrained) < len(build(conn, ids["p01"])), name


def test_violation_count_counts_a_returned_job_that_the_independent_checker_flags_for_the_profile(world):
    ids = load_tiny_benchmark(world)
    with world.connect() as conn:
        everything = bench_methods.hybrid(conn, ids["p01"])  # p01 has no constraints: p02 may not see some of these

        flagged = bench_methods.violation_count(conn, ids["p02"], everything)

        assert flagged == sum(c.facts.country not in (None, "US", "CA") for c in everything) > 0
        stated = next(c for c in everything if c.facts.country)
        assert checker.violations(stated.facts, checker.Constraints(countries=["ZZ"])) == ["location"]


def test_run_profile_scores_m1_to_m4_and_reports_m5_and_m6_as_not_run_when_no_model_key_is_set(
    world, evals_db, monkeypatch
):
    ids = load_tiny_benchmark(world)
    no_key = settings_for(evals_db, LLM_PROVIDER="anthropic")
    monkeypatch.setattr(config, "get", lambda: no_key)
    clock = bench_run.EmbeddingClock()
    with world.connect() as conn:
        refs = target.job_refs(conn)
        labels = {ref: 3 for ref in list(refs.values())[:5]}

        rows, not_run = bench_run.run_profile(conn, ids["p02"], list(bench_run.NAMES), refs, labels, NOW, clock)

    assert sorted(rows) == ["M1", "M2", "M3", "M4"]
    assert not_run == {"M5": bench_run.NOT_RUN, "M6": bench_run.NOT_RUN}
    assert rows["M3"]["embed_cpu_seconds"] >= 0 and rows["M1"]["embed_cpu_seconds"] == 0
    assert all(r["violations"] == 0 and r["model"] is None for r in rows.values())
    assert bench_run.NOT_RUN == "not run: needs a model key"


def test_m4_puts_a_job_whose_title_the_profile_asks_for_above_one_that_m3_ranked_higher(world):
    wants = {
        "key": "p99",
        "name": "Demo Candidate 99",
        "timezone": "UTC",
        "facts": [{"kind": "skill", "statement": "SQL"}],
        "constraints": [],
        "preferences": [
            {
                "kind": "target_title",
                "value": "analytics engineer",
                "weight": 3,
                "statement": "Analytics engineer roles.",
            }
        ],
    }
    with world.begin() as conn:
        user = bench_load.load_profiles(conn, [wants])["p99"]
        first = factories.make_job(
            conn, title="Support engineer", description="Support.", requirements=[], country="IN"
        )
        second = factories.make_job(
            conn, title="Analytics engineer", description="Data.", requirements=[], country="IN"
        )
        embedding.embed_pending(conn)  # one fixed vector, so M3 ties and orders by job id
    with world.connect() as conn:
        m3 = bench_methods.hybrid(conn, user)
        m4 = bench_methods.hybrid_rules(conn, user, NOW)

    assert [j.job_id for j in m3][:2] == [first.id, second.id]
    assert [j.job_id for j in m4] == [second.id, first.id]
    assert sorted(j.job_id for j in m4) == sorted(j.job_id for j in m3)  # a re-ordering of M3's jobs, nothing added


# ---- M5 and M6 on a fixture with the fake provider ----------------------------------------------------------------

# title, description, requirements, and the verdicts a recording gives them (f2 is the Python and SQL bullet)
JOBS = (
    (
        "Data engineer",
        "Data engineer. You will write Python and SQL every day.",
        [("Python", "required"), ("SQL", "required")],
        [("met", ["f2"]), ("met", ["f2"])],
    ),
    (
        "Analytics engineer",
        "Analytics engineer. You will use Python and Tableau.",
        [("Python", "required"), ("Tableau", "required")],
        [("met", ["f2"]), ("no_evidence", [])],
    ),
    ("Support engineer", "Support engineer. You will use Excel.", [("Excel", "required")], [("no_evidence", [])]),
)


@pytest.fixture
def fixture_world(world, monkeypatch, tmp_path):
    """One invented candidate and three invented postings with requirements; the fake provider answers from recordings
    written in the order the jobs are asked about; the route is given a price so the ledger has a cost to sum."""
    with world.begin() as conn:
        user = factories.make_user(conn)
        factories.make_record(conn, user)
        for title, description, requirements, _ in JOBS:
            factories.make_job(
                conn,
                title=title,
                description=description,
                requirements=requirements,
                country="IN",
                arrangements=("remote",),
            )
        embedding.embed_pending(conn)
    real = registry.route

    def priced(task, settings):
        return dataclasses.replace(
            real(task, settings),
            input_microusd_per_mtok=100_000,
            output_microusd_per_mtok=500_000,
            price_checked_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(registry, "route", priced)

    def record(order, times=1):
        """The answers for these jobs, in this order (and again, `times` over), as the fake provider's file."""
        by_title = {title: answer for title, _, _, answer in JOBS}
        answers = [
            {
                "verdicts": [
                    {"requirement": f"r{n}", "verdict": v, "facts": f} for n, (v, f) in enumerate(by_title[j.title], 1)
                ]
            }
            for _ in range(times)
            for j in order
        ]
        path = tmp_path / "recorded.json"
        path.write_text(json.dumps(answers), encoding="utf-8")
        fake.replay_from(path)

    return user, record


def ledger(conn):
    return conn.execute(
        text("SELECT count(*), sum(input_tokens), sum(output_tokens), sum(cost_microusd) FROM llm_calls")
    ).one()


def test_m5_ranks_the_scored_jobs_by_the_rules_final_score_and_costs_what_the_ledger_recorded(fixture_world, world):
    user, record = fixture_world
    with world.connect() as conn:
        m4 = bench_methods.hybrid_rules(conn, user, NOW)
        record(m4)

        order, scoring, shown = bench_methods.full_cascade(conn, user, m4, now=NOW)

        assert [j.title for j in order] == ["Data engineer", "Analytics engineer", "Support engineer"]
        assert len(scoring.accepted) == 3 and scoring.rejected == scoring.schema_invalid == 0
        assert shown == 1  # only the job that meets both required items reaches the rules' threshold
        calls, tokens_in, tokens_out, cost = ledger(conn)
    assert (calls, tokens_in, tokens_out) == (3, 300, 60)
    assert (
        cost == 60
    )  # 100 tokens in at 0.10 and 20 out at 0.50 dollars per million, rounded up: 20 micro-dollars a call


def test_m6_scores_only_the_jobs_m4_would_show_and_keeps_m4s_order(fixture_world, world):
    user, record = fixture_world
    with world.connect() as conn:
        m4 = bench_methods.hybrid_rules(conn, user, NOW)
        record(m4)

        listed, scoring = bench_methods.score_shown(conn, user, m4, now=NOW)

        assert [j.job_id for j in listed] == [j.job_id for j in m4[:5]]
        assert len(scoring.accepted) == 3
        assert ledger(conn)[0] == 3


def test_run_profile_records_the_model_cost_per_method_from_the_ledger(fixture_world, world):
    user, record = fixture_world
    with world.connect() as conn:
        m4 = bench_methods.hybrid_rules(conn, user, NOW)
        record(m4, times=2)  # M5 asks about the three jobs, then M6 does
        refs = target.job_refs(conn)

        rows, not_run = bench_run.run_profile(
            conn, user, ["M4", "M5", "M6"], refs, {refs[j.job_id]: 3 for j in m4}, NOW, bench_run.EmbeddingClock()
        )

    assert not_run == {} and sorted(rows) == ["M4", "M5", "M6"]
    for name in ("M5", "M6"):
        assert rows[name]["model"] == {"calls": 3, "input_tokens": 300, "output_tokens": 60, "cost_microusd": 60}
        assert rows[name]["accepted"] == 3 and rows[name]["violations"] == 0
    assert rows["M5"]["shown_by_rules"] == 1 and rows["M4"]["model"] is None
    assert bench_run.summarize([rows["M5"]])["cost_per_candidate_day_usd"] == pytest.approx(0.00006)


def test_a_fixture_job_whose_requirements_are_not_extracted_makes_m5_report_that_instead_of_an_empty_list(world):
    with world.begin() as conn:
        user = factories.make_user(conn)
        factories.make_record(conn, user)
        factories.make_job(
            conn, title="Data engineer", description="Data engineer. Python.", requirements=[], country="IN"
        )
        embedding.embed_pending(conn)
    with world.connect() as conn:
        m4 = bench_methods.hybrid_rules(conn, user, NOW)

        with pytest.raises(bench_methods.RequirementsMissing):
            bench_methods.full_cascade(conn, user, m4, now=NOW)

        rows, not_run = bench_run.run_profile(
            conn, user, ["M4", "M5"], target.job_refs(conn), {}, NOW, bench_run.EmbeddingClock()
        )

    assert not_run == {"M5": bench_run.NO_REQUIREMENTS} and sorted(rows) == ["M4"]


# ---- the recorded run and its report --------------------------------------------------------------------------------


def test_the_recorded_run_holds_every_pre_registered_bar_and_counts_what_the_label_files_hold():
    run, methods = RESULTS["run"], RESULTS["methods"]

    assert run["label_status"] == MARKER and run["config"]["labels"] == MARKER
    assert (run["profiles"], run["pairs"]) == (len(PROFILES), len(POOL))
    assert run["grades"] == {str(g): sum(x["grade"] == g for x in LABELS) for g in range(4)}
    assert RESULTS["bars"] == {
        "constraint_violations_zero": True,
        "m1_m4_lists_complete": True,
        "m1_m4_unjudged_at_5_zero": True,
        "pool_labelled": True,
        "score": 1.0,
    }
    assert sorted(methods) == ["M1", "M2", "M3", "M4"]
    assert all(methods[m]["slices"]["all"]["constraint_violations"] == 0 for m in methods)
    assert all(methods[m]["slices"]["all"]["cost_per_candidate_day_usd"] == 0 for m in methods)
    assert RESULTS["not_run"] == {"M5": bench_run.NOT_RUN, "M6": bench_run.NOT_RUN}
    assert run["eval_run_id"]


def test_the_recorded_means_and_intervals_are_the_ones_the_per_profile_rows_and_the_seed_give():
    keys = sorted(RESULTS["profiles"])
    for m, entry in RESULTS["methods"].items():
        values = [RESULTS["profiles"][k][m]["ndcg_at_5"] for k in keys]
        recorded = entry["slices"]["all"]["ndcg_at_5"]
        assert recorded["mean"] == pytest.approx(metrics.mean(values))
        assert recorded["ci"] == pytest.approx(list(metrics.bootstrap(values)))
        if m != "M3":
            base = [RESULTS["profiles"][k]["M3"]["ndcg_at_5"] for k in keys]
            difference, (low, high) = metrics.paired_difference(values, base)
            versus = entry["slices"]["all"]["versus_m3"]["ndcg_at_5"]
            assert versus["mean_difference"] == pytest.approx(difference)
            assert versus["ci"] == pytest.approx([low, high]) and versus["detected"] == (not low <= 0 <= high)


def test_the_recorded_per_profile_scores_are_the_ones_the_labels_give_for_the_jobs_each_method_listed():
    labels = {(x["profile_key"], x["job_ref"]): x["grade"] for x in LABELS}
    for key, row in RESULTS["profiles"].items():
        mine = {ref: g for (k, ref), g in labels.items() if k == key}
        for m in RESULTS["methods"]:
            got = metrics.score_list(row[m]["top"], mine)
            assert row[m]["unjudged_at_5"] == 0 == got["unjudged_at_5"], (key, m)
            assert row[m]["precision_at_5"] == pytest.approx(got["precision_at_5"]), (key, m)
            assert row[m]["ndcg_at_5"] == pytest.approx(got["ndcg_at_5"]), (key, m)


def test_the_committed_report_is_the_committed_results_rendered_and_carries_the_caveats():
    rendered = report.render(RESULTS)

    assert rendered == (BENCH / "REPORT.md").read_text(encoding="utf-8")
    for needle in (MARKER, "not run: needs a model key", "ndcg_at_5", "precision_at_5", "recall_at_300"):
        assert needle in rendered
    for needle in ("95% interval", "no detectable difference", "Expectations", "latency p50 / p95", "unjudged at 5"):
        assert needle in rendered
    for needle in (
        "M1 Keyword",
        "M2 Vector",
        "M3 Hybrid",
        "M4 Hybrid plus rules",
        "| M5 Full cascade |",
        "| M6 Score only what is shown |",
    ):
        assert needle in rendered
    assert "$0.000000" in rendered  # the methods that call no model cost exactly nothing


def test_the_report_writer_names_a_difference_only_when_its_interval_excludes_zero():
    assert report.reading({"detected": False, "mean_difference": 0.2}) == "no detectable difference"
    assert report.reading({"detected": True, "mean_difference": 0.2}) == "detected: higher than M3"
    assert report.reading({"detected": True, "mean_difference": -0.2}) == "detected: lower than M3"
