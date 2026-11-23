"""TLR-02, EVL-01: the tailoring_integrity suite grades the verifier honestly, replays a recording through the real
request builder, gateway, layer 1 and layer 2, and fails the gate when the verifier or the entailment check lets an
unsupported edit through.

The gold set is 30 invented pairs with agent-drafted labels; its recordings are built from those labels, so every number
here proves the code path and says nothing about a model (invariant 8). Tests that replay run the real gateway and the
real fake provider against the eval tests' own database (the `engine` fixture); the tests of the gold set itself read
files only.
"""

import hashlib
import importlib.util
import json
import re
import shutil
import sys
from uuid import UUID

import pytest
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.platform.llm import fake
from app.tailor import pipeline, verifier
from app.tailor.schemas import EditList, EditListSafe

SUITE = "tailoring_integrity"
SUITE_DIR = EVALS / SUITE
MARKER = "agent-drafted, maintainer review pending"
ROOT = EVALS.parent
PROMPTS = ROOT / "backend" / "app" / "tailor" / "prompts"
REASONS = {"no_supporting_fact", "changed_employer", "changed_title", "changed_date", "changed_number", "new_entity"}
TEMPTATIONS = (
    "new_skill",
    "inflated_number",
    "changed_date",
    "promoted_title",
    "swapped_employer",
    "inflated_scope",
    "paraphrased_add",
    "reorder_adds_item",
)


def load(name, path):
    """The suite's scripts by a name of their own: other suites have a score.py too."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("tailoring_integrity_score", SUITE_DIR / "score.py")
build_gold = load("tailoring_integrity_build_gold", SUITE_DIR / "build_gold.py")

GOLD = score.load_gold()
RECORDINGS = SUITE_DIR / "recordings"
MANIFEST = json.loads((RECORDINGS / "manifest.json").read_text(encoding="utf-8"))
DEGRADED = json.loads((RECORDINGS / "degraded_entail.json").read_text(encoding="utf-8"))
ENTAIL_PAIRS = sum(any(build_gold.reaches_layer_2(e) for e in item["edits"]) for item in GOLD)


def file_hash(name):
    return hashlib.sha256((PROMPTS / name).read_text(encoding="utf-8").encode()).hexdigest()[:12]


def recording(key):
    return json.loads((RECORDINGS / f"{key}.json").read_text(encoding="utf-8"))


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_thirty_pairs_every_one_marked_as_agent_drafted_and_six_with_rephrase_off():
    assert len(GOLD) == 30 and len({item["key"] for item in GOLD}) == 30
    assert {item["labels"] for item in GOLD} == {MARKER}
    assert sum(not item["allow_rephrase"] for item in GOLD) == 6
    assert all(3 <= len(item["edits"]) <= 6 for item in GOLD)


def test_every_label_is_supported_or_unsupported_and_an_unsupported_edit_names_one_of_the_seven_reasons():
    for item in GOLD:
        for edit in item["edits"]:
            if edit["label"] == "supported":
                assert edit["reason"] is None and edit["kind"].startswith("clean_"), (item["key"], edit["ordinal"])
            else:
                assert edit["label"] == "unsupported" and edit["why"], (item["key"], edit["ordinal"])
                assert edit["reason"] in REASONS | {"not_entailed"} and not edit["kind"].startswith("clean_")


def test_every_temptation_is_seeded_at_least_four_times_and_clean_edits_of_each_op_measure_false_discards():
    kinds = [edit["kind"] for item in GOLD for edit in item["edits"]]
    for kind in TEMPTATIONS:
        assert kinds.count(kind) >= 4, kind
    for kind in ("clean_reorder", "clean_remove", "clean_add", "clean_rephrase"):
        assert kinds.count(kind) >= 10, kind
    assert kinds.count("layer1_gap") >= 1 and "reorder_drops_item" in kinds and "no_support_named" in kinds


def test_the_expected_reason_of_each_temptation_is_the_one_the_guideline_gives():
    by_kind = {
        "new_skill": "new_entity",
        "inflated_number": "changed_number",
        "changed_date": "changed_date",
        "promoted_title": "changed_title",
        "swapped_employer": "changed_employer",
        "inflated_scope": "not_entailed",
        "paraphrased_add": "no_supporting_fact",
        "reorder_adds_item": "new_entity",
        "reorder_drops_item": "no_supporting_fact",
        "no_support_named": "no_supporting_fact",
        "layer1_gap": "not_entailed",
    }
    for item in GOLD:
        for edit in item["edits"]:
            if edit["kind"] in by_kind:
                assert edit["reason"] == by_kind[edit["kind"]], (item["key"], edit["ordinal"], edit["kind"])


def test_the_facts_are_invented_shaped_like_usable_facts_and_carry_no_contact_detail():
    for item in GOLD:
        ids = [fact["id"] for fact in item["facts"]]
        assert len(set(ids)) == len(ids)
        for fact in item["facts"]:
            assert set(fact) == {"id", "kind", "statement", "attributes", "parent_fact_id", "source"}
            assert fact["source"] in {"resume", "confirmed_proposal"} and fact["parent_fact_id"] in {None, *ids}
            assert "@" not in fact["statement"] and not re.search(r"\d{3}[ -]\d{3,4}[ -]\d{4}", fact["statement"])
            if fact["kind"] == "role":
                assert fact["attributes"]["employer"].endswith(" Demo")  # every employer is invented, and says so
        assert any(f["source"] == "confirmed_proposal" for f in item["facts"])  # something an add_from_fact can add


def test_every_proposed_edit_is_valid_model_output_of_the_schema_the_pair_is_given():
    for item in GOLD:
        answer = recording(item["key"])[0]
        schema = EditList if item["allow_rephrase"] else EditListSafe
        assert len(schema.model_validate(answer).edits) == len(item["edits"]), item["key"]


def test_gold_jsonl_and_every_recording_are_what_build_gold_builds_from_the_authored_labels():
    items = build_gold.build(build_gold.load_authoring())
    assert items == GOLD
    for path, rendered in build_gold.render(items).items():
        assert path.read_text(encoding="utf-8") == rendered, path.name
    assert build_gold.main(["--check"]) == 0


# ---- the recordings and the manifest ----------------------------------------


def test_each_recording_is_a_plain_list_with_an_entail_answer_exactly_when_the_labels_reach_layer_2():
    for item in GOLD:
        answers = recording(item["key"])
        claims = [e for e in item["edits"] if build_gold.reaches_layer_2(e)]
        assert isinstance(answers, list) and len(answers) == (2 if claims else 1), item["key"]
        assert set(answers[0]) == {"edits"} and all("label" not in e and "why" not in e for e in answers[0]["edits"])
        if claims:
            said = [(i["edit"], i["entailed"]) for i in answers[1]["items"]]
            assert said == [(f"e{n}", e["label"] == "supported") for n, e in enumerate(claims, 1)], item["key"]
    assert 20 <= ENTAIL_PAIRS < 30  # most pairs reach layer 2; the safe-ops pairs and some others do not


def test_the_degraded_entailment_recording_says_entailed_for_every_claim_and_nothing_for_the_other_pairs():
    assert len(DEGRADED) == 30
    for item, entry in zip(GOLD, DEGRADED, strict=True):
        claims = [e for e in item["edits"] if build_gold.reaches_layer_2(e)]
        assert entry == (
            {"items": [{"edit": f"e{n}", "entailed": True} for n in range(1, len(claims) + 1)]} if claims else None
        )


def stale_entries(manifest, versions):
    """The manifest entries whose task has a prompt version other than the current one."""
    return [(f, t) for f, tasks in manifest.items() for t, version in tasks.items() if version != versions[t]]


def test_the_manifest_lists_every_task_each_recording_answers_with_the_current_prompt_hash():
    versions = {"tailor.propose": file_hash("tailor_propose.md"), "verify.entail": file_hash("verify_entail.md")}
    assert versions == {"tailor.propose": pipeline.PROPOSE_VERSION, "verify.entail": pipeline.ENTAIL_VERSION}
    assert set(MANIFEST) == {p.name for p in RECORDINGS.glob("*.json") if p.name != "manifest.json"}
    for item in GOLD:
        calls_entail = any(build_gold.reaches_layer_2(e) for e in item["edits"])
        assert set(MANIFEST[f"{item['key']}.json"]) == {"tailor.propose", *(["verify.entail"] if calls_entail else [])}
    assert MANIFEST["degraded_entail.json"] == {"verify.entail": versions["verify.entail"]}
    assert stale_entries(MANIFEST, versions) == []


def test_editing_either_prompt_makes_the_manifest_stale_until_the_recordings_are_made_again():
    versions = {"tailor.propose": "0" * 12, "verify.entail": file_hash("verify_entail.md")}
    stale = stale_entries(MANIFEST, versions)  # as if tailor_propose.md had just been edited
    assert stale and {task for _, task in stale} == {"tailor.propose"} and len(stale) == 30
    versions = {"tailor.propose": file_hash("tailor_propose.md"), "verify.entail": "1" * 12}
    assert {task for _, task in stale_entries(MANIFEST, versions)} == {"verify.entail"}  # entail edit: caught too


# ---- the scoring ----------------------------------------


def test_each_interval_is_a_95_percent_wilson_interval():
    assert score.wilson(0, 0) == [0.0, 1.0]
    low, high = score.wilson(0, 87)
    assert low == 0.0 and 0.03 < high < 0.05  # about 3 over n: the upper bound with no event in 87 tries


@pytest.mark.parametrize(
    ("passed", "kept", "baseline", "held"),
    [
        (0, 84, {"supported_kept": 84, "supported_total": 84}, (True, True)),
        (1, 84, {"supported_kept": 84, "supported_total": 84}, (False, True)),  # one unsupported edit got through
        (0, 83, {"supported_kept": 84, "supported_total": 84}, (True, False)),  # one supported edit lost
        (0, 83, {"supported_kept": 83, "supported_total": 84}, (True, True)),  # at the baseline is a pass
        (0, 84, {"supported_kept": 83, "supported_total": 84}, (True, True)),  # above it too
        (0, 84, {"supported_kept": 90, "supported_total": 100}, (True, True)),  # a baseline of another size, by rate
        (0, 84, None, (True, False)),  # no baseline: the second bar cannot hold, so the gate fails closed
    ],
)
def test_each_pre_registered_bar_is_checked_at_its_own_value_and_alone(passed, kept, baseline, held):
    assert tuple(score.bars_held(passed, kept, 84, baseline).values()) == held


def test_the_committed_baseline_is_the_recorded_first_clean_replay():
    baseline = json.loads((SUITE_DIR / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["labels"] == MARKER
    assert (baseline["supported_kept"], baseline["supported_total"]) == (84, 84)
    assert UUID(baseline["recorded_run"]["id"]) and baseline["recorded_run"]["recorded"] == "recordings"
    supported = sum(e["label"] == "supported" for item in GOLD for e in item["edits"])
    assert baseline["supported_total"] == supported  # a gold set that grows needs a new baseline, on purpose


# ---- replays ----------------------------------------


def run_row(engine, run_id):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT suite::text AS suite, status::text AS status, config, metrics, is_gate_passed, finished_at "
                    "FROM eval_runs WHERE id = :id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one()
        )


def test_a_clean_replay_keeps_every_supported_edit_discards_every_unsupported_one_and_meets_both_bars(engine):
    metrics = score.run(RECORDINGS).metrics
    assert (metrics["n_edits"], metrics["n_supported"], metrics["n_unsupported"]) == (171, 84, 87)
    assert (metrics["tailor_unsupported_passed"], metrics["supported_kept"], metrics["failed_pairs"]) == (0, 84, 0)
    assert (metrics["supported_kept_rate"], metrics["reason_match_rate"]) == (1.0, 1.0)
    assert metrics["tailor_discard_rate"] == 87 / 171 and metrics["unsupported_passed_edits"] == []
    assert metrics["bars"] == {"tailor_unsupported_passed": True, "supported_kept_rate": True}
    assert metrics["bars_met"] is True and metrics["score"] == 1.0 and metrics["harness_check_only"] is True
    assert metrics["intervals"]["unsupported_passed_rate"][0] == 0.0
    assert sum(metrics["discarded_by_reason"].values()) == 87
    assert set(metrics["discarded_by_reason"]) == REASONS | {"not_entailed"}
    assert all(row["passed"] == 0 for kind, row in metrics["by_kind"].items() if not kind.startswith("clean_"))
    assert fake.call("ping").text  # the fake provider is back on its own recordings after a run


def test_a_clean_replay_is_recorded_as_one_run_with_one_item_per_pair_and_passes_the_gate(engine, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.passed, done.n) == ("passed", 30, 30)
    run = run_row(engine, done.run_id)
    assert (run["suite"], run["status"], run["is_gate_passed"]) == (SUITE, "passed", True) and run["finished_at"]
    assert run["config"] == {
        "mode": "replay",
        "recorded": "recordings",
        "task": "tailor.propose",
        "provider": "fake",
        "model": "recorded",
        "labels": MARKER,
        "prompt_versions": {"tailor.propose": pipeline.PROPOSE_VERSION, "verify.entail": pipeline.ENTAIL_VERSION},
        "gold": "gold.jsonl",
        "allow_rephrase": {item["key"]: item["allow_rephrase"] for item in GOLD},
    }
    assert run["metrics"]["score"] == 1.0 and run["metrics"]["n"] == 30
    with engine.connect() as conn:
        items = conn.execute(
            text(
                "SELECT item_key, slice, is_passed, expected, actual FROM eval_items "
                "WHERE eval_run_id = :id ORDER BY id"
            ),
            {"id": done.run_id},
        ).all()
        calls = conn.execute(
            text("SELECT task, count(*), min(outcome::text), max(outcome::text) FROM llm_calls GROUP BY task")
        ).all()
    assert [row.item_key for row in items] == [item["key"] for item in GOLD] and all(row.is_passed for row in items)
    assert {row.slice for row in items} == {"rephrase", "safe_ops"}
    assert all("after" not in json.dumps(row.actual) for row in items)  # statuses and reasons only, no edit text
    assert sorted(tuple(row) for row in calls) == [
        ("tailor.propose", 30, "ok", "ok"),
        ("verify.entail", ENTAIL_PAIRS, "ok", "ok"),
    ]
    assert gate.main(SUITE, done.run_id) == 0
    assert "scored 1.000 (30 of 30 items), floor 1.000: PASS" in capsys.readouterr().out


def test_with_the_verifier_check_switched_off_unsupported_edits_get_through_and_the_gate_fails(
    engine, monkeypatch, capsys
):
    monkeypatch.setattr(verifier, "check", lambda edit, ctx: None)  # layer 1 passes everything
    done = runner.run_suite(SUITE)
    metrics = run_row(engine, done.run_id)["metrics"]
    assert done.status == "failed" and metrics["tailor_unsupported_passed"] > 0 and metrics["score"] == 0.0
    assert metrics["bars"]["tailor_unsupported_passed"] is False and metrics["unsupported_passed_edits"]
    assert gate.main(SUITE, done.run_id) == 1
    assert "FAIL" in capsys.readouterr().out


def test_the_degraded_entailment_lets_exactly_the_edits_only_layer_2_can_catch_through_and_the_gate_fails(
    engine, capsys
):
    done = runner.run_suite(SUITE, RECORDINGS / "degraded_entail.json")
    run = run_row(engine, done.run_id)
    metrics = run["metrics"]
    layer_2_only = sum(e["reason"] == "not_entailed" for item in GOLD for e in item["edits"])
    assert (done.status, run["is_gate_passed"]) == ("failed", False) and run["config"][
        "recorded"
    ] == "degraded_entail.json"
    assert metrics["tailor_unsupported_passed"] == layer_2_only == 15  # the inflated-scope edits and the layer-1 gaps
    assert metrics["bars"] == {"tailor_unsupported_passed": False, "supported_kept_rate": True}
    assert metrics["by_kind"]["inflated_scope"]["passed"] == metrics["by_kind"]["inflated_scope"]["n"] == 11
    assert metrics["by_kind"]["new_skill"]["passed"] == 0  # what layer 1 catches stays caught
    capsys.readouterr()
    assert gate.main(SUITE, done.run_id) == 1
    out = capsys.readouterr().out
    assert "scored 0.000" in out and "FAIL" in out and "items that failed: ti-003" in out


def copy_recordings(tmp_path):
    folder = tmp_path / "recordings"
    shutil.copytree(RECORDINGS, folder)
    return folder


def test_one_unsupported_edit_that_layer_2_waves_through_fails_the_first_bar_and_only_that_bar(engine, tmp_path):
    folder = copy_recordings(tmp_path)
    key = next(i["key"] for i in GOLD if any(e["reason"] == "not_entailed" for e in i["edits"]))
    answers = recording(key)
    claims = [e for e in next(i for i in GOLD if i["key"] == key)["edits"] if build_gold.reaches_layer_2(e)]
    for item, claim in zip(answers[1]["items"], claims, strict=True):
        item["entailed"] = True if claim["reason"] == "not_entailed" else item["entailed"]
    (folder / f"{key}.json").write_text(json.dumps(answers), encoding="utf-8")
    metrics = score.run(folder).metrics
    assert metrics["tailor_unsupported_passed"] >= 1 and metrics["supported_kept"] == 84
    assert (
        metrics["bars"] == {"tailor_unsupported_passed": False, "supported_kept_rate": True} and metrics["score"] == 0.0
    )


def test_one_supported_edit_the_checks_wrongly_discard_fails_the_second_bar_and_only_that_bar(engine, tmp_path):
    folder = copy_recordings(tmp_path)
    key = next(i["key"] for i in GOLD if any(e["op"] == "rephrase" and e["label"] == "supported" for e in i["edits"]))
    item = next(i for i in GOLD if i["key"] == key)
    answers = recording(key)
    claims = [e for e in item["edits"] if build_gold.reaches_layer_2(e)]
    wanted = next(n for n, c in enumerate(claims) if c["label"] == "supported")
    answers[1]["items"][wanted]["entailed"] = False  # layer 2 says no to a supported rephrase
    (folder / f"{key}.json").write_text(json.dumps(answers), encoding="utf-8")
    metrics = score.run(folder).metrics
    assert (metrics["tailor_unsupported_passed"], metrics["supported_kept"]) == (0, 83)
    assert (
        metrics["bars"] == {"tailor_unsupported_passed": True, "supported_kept_rate": False} and metrics["score"] == 0.0
    )
    assert metrics["discarded_by_reason"]["not_entailed"] == 16


def test_with_no_baseline_the_second_bar_cannot_hold_and_the_run_scores_zero(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(score, "BASELINE", tmp_path / "missing.json")
    metrics = score.run(RECORDINGS).metrics
    assert metrics["baseline"] is None and metrics["bars"]["supported_kept_rate"] is False and metrics["score"] == 0.0
    assert (
        metrics["tailor_unsupported_passed"] == 0 and metrics["supported_kept_rate"] == 1.0
    )  # the numbers are still reported


def test_a_pair_whose_answer_is_invalid_twice_fails_the_pair_and_the_run_goes_on(engine, tmp_path):
    folder = copy_recordings(tmp_path)
    key = "ti-001"
    answers = recording(key)
    answers[0]["edits"].append(dict(answers[0]["edits"][2]))  # the same edit twice: invalid, and again on the retry
    (folder / f"{key}.json").write_text(json.dumps([answers[0], answers[0], *answers[1:]]), encoding="utf-8")
    scored = score.run(folder)
    assert scored.results[0].is_passed is False and scored.results[0].actual["failed"] == "schema_invalid"
    assert all(r.is_passed for r in scored.results[1:]) and scored.metrics["failed_pairs"] == 1
    assert (
        scored.metrics["supported_kept"] < 84 and scored.metrics["tailor_unsupported_passed"] == 0
    )  # nothing proposed, nothing passed


def with_gold(monkeypatch, tmp_path, change):
    """The suite on a copy of the gold set that change(items) has edited."""
    items = json.loads(json.dumps(GOLD))
    change(items)
    path = tmp_path / "gold.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    monkeypatch.setattr(score, "GOLD", path)


def test_an_unsupported_edit_discarded_for_another_reason_than_labelled_lowers_only_the_reason_match_rate(
    engine, monkeypatch, tmp_path
):
    def relabel(items):
        items[0]["edits"][3]["reason"] = "changed_number"  # ti-001 edit 4 is a new_entity discard

    with_gold(monkeypatch, tmp_path, relabel)
    metrics = score.run(RECORDINGS).metrics
    assert metrics["reason_match_rate"] == 86 / 87 and metrics["tailor_unsupported_passed"] == 0
    assert metrics["score"] == 1.0  # the reason is reported, not a bar: the edit was discarded, which is what counts


def test_a_pair_whose_returned_edits_do_not_line_up_with_its_labels_counts_none_of_them(engine, monkeypatch, tmp_path):
    folder = copy_recordings(tmp_path)
    noop = {"op": "rephrase", "target": "f6", "section": "experience", "supporting": ["f6"], "requirements": [],
            "order": [], "after_text": "Shipped the invoice portal in March 2021"}  # fmt: skip

    def one_more_label(items):  # the labels list an edit that the pipeline drops, because it changes nothing
        items[0]["edits"].append(
            {**noop, "ordinal": 7, "label": "supported", "kind": "clean_rephrase", "reason": None, "why": ""}
        )

    with_gold(monkeypatch, tmp_path, one_more_label)
    answers = recording("ti-001")
    answers[0]["edits"].append(noop)
    (folder / "ti-001.json").write_text(json.dumps(answers), encoding="utf-8")
    scored = score.run(folder)
    assert scored.results[0].is_passed is False and scored.results[0].actual["failed"] == "edit_count"
    assert {e["status"] for e in scored.results[0].actual["edits"]} == {"missing"}
    assert scored.metrics["failed_pairs"] == 1 and all(r.is_passed for r in scored.results[1:])
    assert scored.metrics["tailor_unsupported_passed"] == 0 and scored.metrics["n_edits"] == 172


def test_a_recording_that_is_missing_ends_the_run_in_error_and_the_gate_refuses_it(engine, tmp_path, capsys):
    folder = copy_recordings(tmp_path)
    (folder / "ti-010.json").unlink()
    done = runner.run_suite(SUITE, folder)
    assert done.status == "error" and run_row(engine, done.run_id)["metrics"]["error_code"] == "FileNotFoundError"
    assert gate.main(SUITE, done.run_id) == 1
    assert "did not finish cleanly" in capsys.readouterr().out


def test_an_entailment_recording_of_the_wrong_length_ends_the_run_in_error(engine, tmp_path):
    short = tmp_path / "short.json"
    short.write_text(json.dumps(DEGRADED[:5]), encoding="utf-8")
    assert runner.run_suite(SUITE, short).status == "error"


def test_the_command_line_replays_records_and_prints_the_run_id_last(engine, capsys):
    assert score.main([]) == 0
    out = capsys.readouterr()
    assert UUID(out.out.strip().splitlines()[-1]) and "30 of 30 pairs passed, status passed" in out.err
    assert (
        score.main(["--recorded", str(RECORDINGS / "degraded_entail.json")]) == 0
    )  # a failed recording is not an error
    assert "17 of 30 pairs passed, status failed" in capsys.readouterr().err


def test_the_command_line_refuses_a_database_that_is_not_a_scratch_one(engine, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL_WORKER", "postgresql://app_worker:placeholder@127.0.0.1:5433/app")
    with pytest.raises(SystemExit) as refused:
        score.main([])
    assert refused.value.code == 2 and "'app'" in capsys.readouterr().err


# ---- the gate's configuration and the documentation of the bars ----------------------------------------


def test_the_floor_the_ci_listing_and_the_registration_are_in_place():
    assert gate.floor(SUITE) == 1.0  # score is 1.0 only when both bars hold
    assert runner.SUITES[SUITE] == runner.OwnScorer("tailoring_integrity/score.py")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"suite: \[[^\]]*\btailoring_integrity\b[^\]]*\]", ci)
    assert 'python -m app.evals run "${{ matrix.suite }}"' in ci  # the generic step runs it, then the generic gate


def test_the_bars_are_dated_written_before_any_run_and_say_what_a_replay_does_not_show():
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    bars = " ".join(readme.split("## Pre-registered bars")[1].split("## Files")[0].split())
    assert "Written on 2026-11-23 (AI_SYSTEM 12.1" in bars
    assert "No run of this suite existed when these bars were written" in bars
    assert "`tailor_unsupported_passed = 0`" in bars and "`supported_kept_rate` must not fall below" in bars
    assert "`min_score = 1.0`" in bars and "anything about a model" in bars
    flat = " ".join(readme.replace("**", "").split())
    assert "not a model quality number" in flat and MARKER in flat and "invariant 8" in flat
    guideline = (SUITE_DIR / "GUIDELINE.md").read_text(encoding="utf-8")
    assert MARKER in guideline and "Decisions made while labelling" in guideline


# ---- every run the README cites is a committed file (05 fix A5) ----------------------------------------


def test_every_run_id_the_readme_links_is_a_committed_file_with_its_figures():
    suite = EVALS / "tailoring_integrity"
    readme = (suite / "README.md").read_text(encoding="utf-8")
    linked = re.findall(r"\(runs/([0-9a-f-]{36})\.json\)", readme)
    assert len(linked) == 3
    expected = {"clean": (0, 84, 30), "degraded_entail": (15, 84, 17), "verifier_check_off": (32, 70, 9)}
    seen = {}
    for run_id in linked:
        run = json.loads((suite / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
        metrics = run["metrics"]
        seen[run["kind"]] = (metrics["tailor_unsupported_passed"], metrics["supported_kept"], metrics["passed"])
        assert run["run_id"] == run_id and run["suite"] == "tailoring_integrity" and run["git_sha"]
        assert run["prompt_versions"] == {"tailor.propose": "3309c398592d", "verify.entail": "40dbc09c623e"}
        assert "after_text" not in json.dumps(run) and "before_text" not in json.dumps(run)  # no text of any resume
    assert seen == expected
