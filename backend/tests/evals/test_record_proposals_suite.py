"""CHT-03, EVL-01 (plan 05-06, task 1): the record_proposals suite runs 20 invented candidate messages through
chat.api.propose with a recorded record.propose answer, and fails the gate when a proposal control breaks (the quote
rule, the equal-value check) or an expected proposal goes missing, or when anything is written without a confirmation.

Agent-drafted labels and hand-written recordings: the numbers prove the checks and the wiring, not a model
(invariant 8).
"""

import json
import shutil
from pathlib import Path

import pytest

from app.chat import loop, proposals
from app.evals import EVALS, gate, runner
from app.platform.llm import fake
from app.profile import api as profile
from tests.evals import p5_env

SUITE = "record_proposals"
SUITE_DIR = EVALS / SUITE
MARKER = "agent-drafted, maintainer review pending"

build = p5_env.load("record_proposals_build", SUITE_DIR / "build.py")
score = p5_env.load("record_proposals_score_module", SUITE_DIR / "score.py")
GOLD = score.load_gold()
MANIFEST = p5_env.read_json(SUITE_DIR / "recordings" / "manifest.json")


@pytest.fixture
def world(engine, evals_db, monkeypatch):
    p5_env.use_private_database(monkeypatch, evals_db)
    return engine


def metrics_of(done, engine):
    return p5_env.run_row(engine, done.run_id)["metrics"]


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_twenty_scripts_marked_as_agent_drafted_with_the_traps_the_plan_names():
    assert len(GOLD) == 20 and len({item["key"] for item in GOLD}) == 20
    assert {item["labels"] for item in GOLD} == {MARKER}
    cases = " ".join(item["case"] for item in GOLD)
    for trap in ("job posting", "protected", "equal to", "declined", "role", "paraphrase", "already holds"):
        assert trap in cases, trap
    assert sum(len(item["expect"]) for item in GOLD) == 12
    assert sum(not item["expect"] for item in GOLD) == 9  # the nine scripts that must store nothing


def test_the_committed_files_are_what_the_authored_source_writes(tmp_path):
    build.write(tmp_path)
    assert (tmp_path / "gold.jsonl").read_text(encoding="utf-8") == (SUITE_DIR / "gold.jsonl").read_text(
        encoding="utf-8"
    )
    written = {p.name for p in (tmp_path / "recordings").glob("*.json")}
    assert written == {p.name for p in (SUITE_DIR / "recordings").glob("*.json")}
    for name in written:
        assert (tmp_path / "recordings" / name).read_text() == (SUITE_DIR / "recordings" / name).read_text(), name


def test_the_manifest_lists_record_propose_for_every_recording_with_the_current_prompt_hash():
    current = p5_env.prompt_hash(Path(loop.__file__).parent / "prompts" / "record_propose.md")
    assert current == proposals.PROMPT_VERSION
    assert set(MANIFEST) == {f"{item['key']}.json" for item in GOLD}
    assert all(tasks == {"record.propose": current} for tasks in MANIFEST.values())


def test_every_recorded_quote_of_a_trap_is_either_absent_from_the_message_or_stopped_by_another_rule():
    by_key = {item["key"]: item for item in GOLD}
    for key in ("rp-10", "rp-15"):  # the two quote traps: the recorded quote is not in the message
        (answer,) = p5_env.read_json(SUITE_DIR / "recordings" / f"{key}.json")
        assert all(p["evidence_quote"] not in by_key[key]["message"] for p in answer["proposals"]), key


# ---- the matching of what was stored to what was expected ----------------------------------------


class Event:
    def __init__(self, entity, operation, after):
        self.entity, self.operation, self.after = entity, operation, after


def test_a_stored_proposal_matches_an_expected_one_on_entity_operation_and_the_named_parts_only():
    want = {"entity": "preference", "operation": "create", "after": {"kind": "location", "value": "pune"}}
    assert score.is_expected(want, Event("preference", "create", {"kind": "location", "value": " Pune ", "weight": 2}))
    assert not score.is_expected(want, Event("preference", "update", {"kind": "location", "value": "Pune"}))
    assert not score.is_expected(want, Event("fact", "create", {"kind": "location", "value": "Pune"}))
    assert not score.is_expected(want, Event("preference", "create", {"kind": "location", "value": "Mumbai"}))
    removal = {"entity": "preference", "operation": "remove", "after": None}
    assert score.is_expected(removal, Event("preference", "remove", None))
    assert not score.is_expected(removal, Event("preference", "remove", {"kind": "target_title"}))


def test_each_expected_proposal_is_used_once_so_a_duplicate_does_not_count_twice():
    want = [{"entity": "fact", "operation": "create", "after": {"kind": "skill"}}]
    twice = [Event("fact", "create", {"kind": "skill"}), Event("fact", "create", {"kind": "skill"})]
    assert score.pair_up(want, twice) == 1


@pytest.mark.parametrize(
    ("precision", "recall", "writes", "held"),
    [
        (1.0, 1.0, 0, {"unconfirmed_writes": True, "proposal_precision": True, "proposal_recall": True}),
        (11 / 12, 1.0, 0, {"unconfirmed_writes": True, "proposal_precision": False, "proposal_recall": True}),
        (1.0, 11 / 12, 0, {"unconfirmed_writes": True, "proposal_precision": True, "proposal_recall": False}),
        (1.0, 1.0, 1, {"unconfirmed_writes": False, "proposal_precision": True, "proposal_recall": True}),
    ],
)
def test_each_pre_registered_bar_is_checked_at_its_own_value_and_alone(precision, recall, writes, held):
    metrics = {"proposal_precision": precision, "proposal_recall": recall, "unconfirmed_writes": writes}
    baseline = {"proposal_precision": 1.0, "proposal_recall": 1.0}
    assert score.bars_held(metrics, baseline) == held


def test_with_no_baseline_the_two_relative_bars_cannot_hold():
    tally = {"stored": 12, "expected": 12, "matched": 12, "writes": 0, "ok": 20}
    metrics = score.metrics_of(tally, 20, None)
    assert metrics["score"] == 0.0 and metrics["bars"]["proposal_precision"] is False


# ---- the replay ----------------------------------------


def test_a_clean_replay_stores_exactly_the_expected_proposals_and_passes_the_gate(world, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.passed, done.n) == ("passed", 20, 20)
    run = p5_env.run_row(world, done.run_id)
    metrics = run["metrics"]
    assert run["is_gate_passed"] is True and run["config"]["labels"] == MARKER
    assert (metrics["proposal_precision"], metrics["proposal_recall"], metrics["unconfirmed_writes"]) == (1.0, 1.0, 0)
    assert (metrics["n_stored"], metrics["n_expected"], metrics["attempts"], metrics["score"]) == (12, 12, 1, 1.0)
    assert gate.main(SUITE, done.run_id) == 0
    assert "scored 1.000 (20 of 20 items), floor 1.000: PASS" in capsys.readouterr().out
    assert fake.call("ping").text


def test_the_committed_baseline_is_the_clean_replay_and_the_floor_is_the_score_it_holds():
    baseline = p5_env.read_json(SUITE_DIR / "baseline.json")
    assert baseline["score"] == 1.0 == gate.floor(SUITE)
    assert (baseline["proposal_precision"], baseline["proposal_recall"], baseline["unconfirmed_writes"]) == (
        1.0,
        1.0,
        0,
    )


# ---- the gate can fail ----------------------------------------


def test_with_the_quote_rule_switched_off_the_quotes_taken_from_elsewhere_get_stored_and_precision_falls(
    world, monkeypatch
):
    real = proposals._check

    def any_quote_will_do(item, user_text, current, declined):
        return real(item, item.evidence_quote, current, declined)  # the quote is always "in" the message

    monkeypatch.setattr(proposals, "_check", any_quote_will_do)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert done.status == "failed" and metrics["proposal_precision"] < 1.0
    assert metrics["bars"]["proposal_precision"] is False and metrics["proposal_recall"] == 1.0
    with world.connect() as conn:
        from sqlalchemy import text

        failed = {
            row.item_key
            for row in conn.execute(
                text("SELECT item_key FROM eval_items WHERE eval_run_id = :r AND NOT is_passed"), {"r": done.run_id}
            )
        }
    assert {"rp-10", "rp-15"} <= failed


def test_with_a_correct_proposal_removed_from_the_recording_recall_falls_and_the_gate_fails(world, tmp_path):
    folder = tmp_path / "recordings"
    shutil.copytree(SUITE_DIR / "recordings", folder)
    (folder / "rp-02.json").write_text(json.dumps([{"proposals": []}]), encoding="utf-8")
    done = runner.run_suite(SUITE, folder)
    metrics = metrics_of(done, world)
    assert done.status == "failed" and metrics["proposal_recall"] == 11 / 12
    assert metrics["proposal_precision"] == 1.0 and metrics["bars"]["proposal_recall"] is False


def test_a_proposal_applied_without_the_candidates_confirmation_is_counted_and_fails_the_gate(world, monkeypatch):
    real = profile.propose_change

    def applying(conn, user_id, **fields):
        event = real(conn, user_id, **fields)
        profile.confirm(conn, user_id, event)
        return event

    monkeypatch.setattr(proposals.profile, "propose_change", applying)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert (
        done.status == "failed"
        and metrics["unconfirmed_writes"] >= 9
        and metrics["bars"]["unconfirmed_writes"] is False
    )


def test_with_the_equal_value_check_switched_off_a_proposal_that_changes_nothing_is_stored(world, monkeypatch):
    real = proposals._plan

    def never_the_same(item, row, after, same):
        return real(item, row, after, False)

    monkeypatch.setattr(proposals, "_plan", never_the_same)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert done.status == "failed" and metrics["proposal_precision"] < 1.0


def test_a_script_whose_recording_is_missing_ends_the_run_in_error_and_the_gate_refuses_it(world, tmp_path, capsys):
    done = runner.run_suite(SUITE, tmp_path)
    assert done.status == "error" and gate.main(SUITE, done.run_id) == 1
    assert "did not finish cleanly" in capsys.readouterr().out


def test_the_floor_the_registration_and_the_readme_are_in_place():
    assert isinstance(runner.SUITES[SUITE], runner.OwnScorer) and gate.floor(SUITE) == 1.0
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    assert MARKER in readme and "not a model quality number" in readme and "Pre-registered bars" in readme
