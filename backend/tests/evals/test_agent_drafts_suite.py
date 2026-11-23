"""CMP-03 to CMP-05, EVL-01 (plan 07-02): the agent_drafts suite replays 10 Writer packets, 10 Chaser drafts and 30
Coach prep packs through the real request builders and validators and fails the gate when a check is switched off.

The gold set is agent-drafted; its recordings are built from the labels, so every number here proves the code path and
says nothing about a model (invariant 8). The replays run on the eval tests' own database (the `engine` fixture); the
tests of the gold set itself read files only.
"""

import filecmp
import importlib.util
import sys
from collections import Counter

from sqlalchemy import text

from app.apply import packet
from app.campaigns.crew import common
from app.evals import EVALS, gate, runner
from app.tailor import api as tailor

SUITE = "agent_drafts"
SUITE_DIR = EVALS / SUITE
MARKER = "agent-drafted, maintainer review pending"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


score = load("agent_drafts_score", SUITE_DIR / "score.py")
build = load("agent_drafts_build", SUITE_DIR / "build.py")
GOLD = score.load_gold()


def metrics_of(engine, run_id):
    with engine.connect() as c:
        return c.execute(text("SELECT metrics FROM eval_runs WHERE id = :i"), {"i": run_id}).scalar_one()


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_fifty_items_marked_as_agent_drafted_ten_writer_ten_chaser_thirty_coach():
    assert len(GOLD) == 50 and len({item["key"] for item in GOLD}) == 50
    assert Counter(item["kind"] for item in GOLD) == {"writer": 10, "chaser": 10, "coach": 30}
    assert {item["labels"] for item in GOLD} == {MARKER}
    assert "wiring" in (SUITE_DIR / "README.md").read_text(encoding="utf-8")  # a replay checks wiring only


def test_every_item_seeds_unsupported_content_and_names_invented_things_only():
    for item in GOLD:
        labelled = item["sentences"] if item["kind"] != "coach" else [*item["questions"], *item["ask_them"]]
        assert any(s["label"] == "unsupported" for s in labelled), item["key"]
        assert any(s["label"] == "supported" for s in labelled), item["key"]
    text_ = (SUITE_DIR / "gold.jsonl").read_text(encoding="utf-8")
    assert "@mail.example" in text_ and "https://portfolio.example" in text_ and "gmail" not in text_.lower()
    writer = [i for i in GOLD if i["kind"] == "writer"]
    assert sum(bool(i["dossier"]) for i in writer) == 7  # campaign packets; the other three are Phase 5 packets


def test_the_committed_files_are_what_the_build_script_writes(tmp_path):
    build.build(tmp_path)
    assert (tmp_path / "gold.jsonl").read_bytes() == (SUITE_DIR / "gold.jsonl").read_bytes()
    same = filecmp.dircmp(tmp_path / "recorded", SUITE_DIR / "recorded")
    assert not (same.left_only or same.right_only or same.diff_files)


# ---- the replay ----------------------------------------


def test_a_clean_replay_meets_every_bar_and_passes_the_gate(engine, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.n, done.passed) == ("passed", 50, 50)
    metrics = metrics_of(engine, done.run_id)
    assert metrics["score"] == 1.0 and metrics["bars_met"] and metrics["harness_check_only"]
    assert metrics["drafts_unsupported_passed"] == 0 and metrics["drafts_restricted_passed"] == 0
    assert metrics["drafts_supported_kept_rate"] >= 0.90 and metrics["drafts_candidate_only_blank"] == 1.0
    assert metrics["prep_items_without_fact"] == 0 and metrics["n_supported"] > 100
    assert gate.main(SUITE, done.run_id) == 0
    assert "PASS" in capsys.readouterr().out


def test_the_degraded_fixture_switches_every_validator_to_pass_through_and_the_gate_fails(engine, capsys):
    done = runner.run_suite(SUITE, SUITE_DIR / "degraded" / "pass_through.json")
    metrics = metrics_of(engine, done.run_id)
    assert (done.status, metrics["score"]) == ("failed", 0.0)
    assert metrics["drafts_unsupported_passed"] > 0 and metrics["drafts_restricted_passed"] > 0
    assert gate.main(SUITE, done.run_id) == 1
    assert "FAIL" in capsys.readouterr().out
    assert (
        tailor.unsupported_reason("I also know Kubernetes.", [], skills=[], job_terms=[]) == "no_supporting_fact"
    )  # put back


def test_with_the_entailment_check_switched_off_the_overclaims_are_kept_and_the_gate_fails(engine, monkeypatch):
    monkeypatch.setattr(tailor, "entailed", lambda items, **k: [True] * len(list(items)))
    done = runner.run_suite(SUITE)
    assert metrics_of(engine, done.run_id)["drafts_unsupported_passed"] > 0
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_with_the_restricted_content_check_switched_off_contact_details_and_pay_are_kept_and_the_gate_fails(
    engine, monkeypatch
):
    monkeypatch.setattr(common, "restricted_reason", lambda text_: None)
    done = runner.run_suite(SUITE)
    assert metrics_of(engine, done.run_id)["drafts_restricted_passed"] > 0
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_with_the_candidate_only_questions_opened_up_the_writer_drafts_pay_and_the_gate_fails(engine, monkeypatch):
    open_all = tuple(packet.Question(q.id, q.text, True) for q in packet.PACKET_QUESTIONS)
    monkeypatch.setattr(packet, "PACKET_QUESTIONS", open_all)
    done = runner.run_suite(SUITE)
    assert metrics_of(engine, done.run_id)["drafts_candidate_only_blank"] < 1.0
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_with_layer_one_switched_off_a_changed_number_is_kept_and_the_gate_fails(engine, monkeypatch):
    monkeypatch.setattr(tailor, "unsupported_reason", lambda *a, **k: None)
    done = runner.run_suite(SUITE)
    assert (done.status, gate.main(SUITE, done.run_id)) == ("failed", 1)


def test_an_item_whose_recording_is_missing_ends_the_run_in_error_and_the_gate_refuses_it(engine, tmp_path):
    done = runner.run_suite(SUITE, tmp_path)
    assert done.status == "error" and gate.main(SUITE, done.run_id) == 1


def test_the_floor_and_the_registry_are_in_place():
    assert gate.floor(SUITE) == 1.0 and isinstance(runner.SUITES[SUITE], runner.OwnScorer)
