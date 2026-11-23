"""CHT-01 to CHT-04, EVL-01 (plan 05-06, task 1): the chat suite sends 20 invented scripts as person A through the real
turn loop and the real tools, a recorded model, and an invented seeded world, and fails the gate when answers stop
citing, when a write is not confirmed, when anything of the other person leaves, or when search stops applying the
person's hard constraints.

The scripts and recordings are agent-drafted and written by hand against the seeded world, so every number here proves
the wiring and that a fault fails; none is a model quality number (invariant 8). The tests run the real gateway on the
fake provider against the eval tests' own private database.
"""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.chat import loop, proposals, schemas, tools
from app.evals import EVALS, gate, runner
from app.platform.llm import fake
from app.profile import api as profile
from tests.evals import p5_env

SUITE = "chat"
SUITE_DIR = EVALS / SUITE
MARKER = "agent-drafted, maintainer review pending"
KINDS = {"decision", "job", "fact", "constraint"}

build = p5_env.load("chat_build", SUITE_DIR / "build.py")
score = p5_env.load("chat_score_module", SUITE_DIR / "score.py")
seed = score.seed
GOLD = score.load_gold()
MANIFEST = p5_env.read_json(SUITE_DIR / "recordings" / "manifest.json")


@pytest.fixture
def world(engine, evals_db, monkeypatch):
    """The private database under all four roles, with the fake provider; the seed writes there."""
    p5_env.use_private_database(monkeypatch, evals_db)
    return engine


def metrics_of(done, engine):
    return p5_env.run_row(engine, done.run_id)["metrics"]


# ---- the gold set ----------------------------------------


def test_the_gold_set_is_twenty_scripts_each_marked_as_agent_drafted_and_each_with_a_recording():
    assert len(GOLD) == 20 and len({item["key"] for item in GOLD}) == 20
    assert {item["labels"] for item in GOLD} == {MARKER}
    for item in GOLD:
        assert set(item["cites"]) <= KINDS and item["message"].strip(), item["key"]
        answers = p5_env.read_json(SUITE_DIR / "recordings" / f"{item['key']}.json")
        assert answers and all(isinstance(a, dict) for a in answers), item["key"]


def test_the_scripts_cover_facts_constraints_matches_a_filtered_decision_searches_other_people_send_it_and_proposals():
    tool_sets = [set(item["tools"]) for item in GOLD]
    assert sum("get_record" in t for t in tool_sets) >= 4
    assert sum("get_decision" in t for t in tool_sets) >= 3
    assert sum(item["searches"] for item in GOLD) == 3
    assert sum(item["proposal"] for item in GOLD) == 3
    messages = " ".join(item["message"].lower() for item in GOLD)
    assert "other candidate" in messages and "yes, send it" in messages and "ruled out" in messages
    assert sum(item["context_job"] is not None for item in GOLD) == 1 and sum(bool(i["history"]) for i in GOLD) == 2


def test_the_committed_files_are_what_the_authored_source_writes(tmp_path):
    build.write(tmp_path)
    for name in ("gold.jsonl", "degraded.json", "recordings/manifest.json"):
        assert (tmp_path / name).read_text(encoding="utf-8") == (SUITE_DIR / name).read_text(encoding="utf-8"), name
    written = {p.name for p in (tmp_path / "recordings").glob("*.json")}
    assert written == {p.name for p in (SUITE_DIR / "recordings").glob("*.json")}
    for name in written:
        assert (tmp_path / "recordings" / name).read_text() == (SUITE_DIR / "recordings" / name).read_text(), name


def test_the_manifest_lists_every_task_each_recording_answers_with_the_current_prompt_hash():
    versions = {
        "chat.turn": p5_env.prompt_hash(Path(loop.__file__).parent / "prompts" / "chat_turn.md"),
        "record.propose": p5_env.prompt_hash(Path(loop.__file__).parent / "prompts" / "record_propose.md"),
    }
    assert versions == {"chat.turn": loop.PROMPT_VERSION, "record.propose": proposals.PROMPT_VERSION}
    for item in GOLD:
        expected = {"chat.turn", *(["record.propose"] if item["proposal"] else [])}
        assert set(MANIFEST[f"{item['key']}.json"]) == expected, item["key"]
        assert all(MANIFEST[f"{item['key']}.json"][task] == versions[task] for task in expected)


def test_the_degraded_recording_is_one_bare_reply_with_no_tool_call_per_script():
    degraded = p5_env.read_json(SUITE_DIR / "degraded.json")
    assert len(degraded) == len(GOLD)
    assert all(step["tool_calls"] == [] and "[[" not in step["reply"] for step in degraded)


# ---- the seed's guard (INDEX decision 32, T-05-46) ----------------------------------------


def test_the_seed_refuses_a_url_that_names_no_test_database_before_any_connection_attempt(monkeypatch):
    # port 1 refuses a connection: reaching the network would raise OperationalError, not the refusal
    monkeypatch.setenv("DATABASE_URL_MIGRATE", "postgresql://x@127.0.0.1:1/app")
    with pytest.raises(seed.NotATestDatabase, match="ending in _test"):
        seed.open_database()
    monkeypatch.setenv("DATABASE_URL_MIGRATE", "postgresql://app:app_local_only@127.0.0.1:5433/app")  # the default
    with pytest.raises(seed.NotATestDatabase):
        seed.open_database()
    monkeypatch.delenv("DATABASE_URL_MIGRATE")
    with pytest.raises(seed.NotATestDatabase):
        seed.open_database()


def test_the_seed_refuses_a_database_whose_current_database_lacks_the_suffix_before_writing_a_row(world, evals_db):
    name = f"p5_guard_probe_{os.getpid()}"  # no _test: it must be refused once connected
    admin = create_engine(
        evals_db.set(drivername="postgresql+psycopg", database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        probe = create_engine(evals_db.set(drivername="postgresql+psycopg", database=name))
        with probe.begin() as conn:
            with pytest.raises(seed.NotATestDatabase, match="current_database"):
                seed.seed(conn)  # an empty database: a write would raise UndefinedTable, not the refusal
        probe.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_the_seed_writes_two_people_five_postings_and_a_canary_that_is_new_on_every_run(world):
    engine = seed.open_database()
    with engine.begin() as conn:
        first = seed.seed(conn)
    with engine.begin() as conn:
        second = seed.seed(conn)
    assert first.canary != second.canary and first.a != second.a and first.ok.id != second.ok.id
    with world.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in ("users", "jobs")}
        secret = conn.execute(
            text("SELECT user_id FROM profile_facts WHERE statement LIKE :c"), {"c": f"%{second.canary}%"}
        )
        assert [row.user_id for row in secret] == [second.b]
    assert counts == {"users": 2, "jobs": 5}  # the second run emptied the first
    engine.dispose()


# ---- the replay ----------------------------------------


def test_a_clean_replay_cites_every_answer_writes_nothing_leaks_nothing_and_passes_the_gate(world, capsys):
    done = runner.run_suite(SUITE)
    assert (done.status, done.passed, done.n) == ("passed", 20, 20)
    run = p5_env.run_row(world, done.run_id)
    metrics = run["metrics"]
    assert (run["suite"], run["is_gate_passed"]) == (SUITE, True)
    assert (metrics["citations_present"], metrics["citations_valid"]) == (1.0, 1.0)
    assert (metrics["unconfirmed_writes"], metrics["cross_user_data"]) == (0, 0)
    assert (metrics["chat_task_success_pass1"], metrics["attempts"], metrics["n"], metrics["score"]) == (
        1.0,
        1,
        20,
        1.0,
    )
    assert metrics["n_citations"] >= 20 and metrics["harness_check_only"] is True
    assert run["config"]["labels"] == MARKER and run["config"]["attempts"] == 1
    assert gate.main(SUITE, done.run_id) == 0
    assert "scored 1.000 (20 of 20 items), floor 1.000: PASS" in capsys.readouterr().out
    assert fake.call("ping").text  # the fake provider is back on its own recordings after a run


def test_the_committed_baseline_is_the_clean_replay_and_the_floor_is_the_score_it_holds():
    baseline = p5_env.read_json(SUITE_DIR / "baseline.json")
    assert baseline["score"] == 1.0 == gate.floor(SUITE)
    assert (baseline["chat_task_success_pass1"], baseline["citations_present"], baseline["citations_valid"]) == (
        1.0,
    ) * 3
    assert (baseline["unconfirmed_writes"], baseline["cross_user_data"], baseline["attempts"]) == (0, 0, 1)


def test_the_replay_fetches_no_embedding_model_even_when_a_search_runs(world, monkeypatch):
    from app.platform.llm import embed

    def boom():
        raise AssertionError("the fastembed model was asked for")

    monkeypatch.setattr(embed, "_model", boom)
    done = runner.run_suite(SUITE)
    assert done.status == "passed"


# ---- the gate can fail ----------------------------------------


def test_the_degraded_recording_answers_without_calling_a_tool_or_citing_and_the_gate_fails(world, capsys):
    done = runner.run_suite(SUITE, SUITE_DIR / "degraded.json")
    assert done.status == "failed" and done.passed < 20
    metrics = metrics_of(done, world)
    assert metrics["citations_present"] == 0.0 and metrics["score"] == 0.0
    assert metrics["bars"]["citations_present"] is False and metrics["bars"]["chat_task_success_pass1"] is False
    assert gate.main(SUITE, done.run_id) == 1
    assert "FAIL" in capsys.readouterr().out


def test_with_the_constraint_filter_of_search_switched_off_a_violator_is_returned_and_the_gate_fails(
    world, monkeypatch
):
    real = tools.ingest.search

    def unconstrained(conn, **fields):
        return real(conn, **{**fields, "constrained_for": None})

    monkeypatch.setattr(tools.ingest, "search", unconstrained)
    done = runner.run_suite(SUITE)
    assert done.status == "failed"
    with world.connect() as conn:
        failed = {
            row.item_key: row.actual
            for row in conn.execute(
                text("SELECT item_key, actual FROM eval_items WHERE eval_run_id = :r AND NOT is_passed"),
                {"r": done.run_id},
            )
        }
    assert {"chat-09", "chat-11"} <= set(failed)  # the analyst search (chat-10) never matched a violator
    assert all("search_constrained" in actual["failed_checks"] for actual in failed.values())


def test_a_proposal_that_is_applied_without_the_candidates_confirmation_is_counted_and_fails_the_gate(
    world, monkeypatch
):
    real = profile.propose_change

    def applying(conn, user_id, **fields):
        event = real(conn, user_id, **fields)
        profile.confirm(conn, user_id, event)  # what no code may do: the candidate never answered
        return event

    monkeypatch.setattr(proposals.profile, "propose_change", applying)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert (
        done.status == "failed"
        and metrics["unconfirmed_writes"] == 3
        and metrics["bars"]["unconfirmed_writes"] is False
    )


def test_a_tool_that_lets_the_other_persons_canary_out_is_counted_and_fails_the_gate(world, monkeypatch):
    real = tools.get_record

    def leaky(user_id, args, aliases):
        output = real(user_id, args, aliases)
        with world.connect() as conn:
            canary = conn.execute(
                text("SELECT statement FROM profile_facts WHERE statement LIKE '%canary-%'")
            ).scalar_one()
        return schemas.ToolOutput({**output.data, "extra": canary}, output.refs, output.source_kind)

    monkeypatch.setattr(tools, "get_record", leaky)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert done.status == "failed" and metrics["cross_user_data"] >= 4 and metrics["bars"]["cross_user_data"] is False


def test_a_citation_that_resolves_to_a_row_of_nobody_makes_citations_valid_fall_below_one(world, monkeypatch):
    from app.chat import refs

    real = refs.resolve

    def forged(text_, table):
        resolved = real(text_, table)
        fake_citation = refs.Citation("d1", "decision", "00000000-0000-0000-0000-000000000000")
        return refs.Resolved(resolved.text, (*resolved.citations, fake_citation), resolved.unknown)

    monkeypatch.setattr(loop.refs, "resolve", forged)
    done = runner.run_suite(SUITE)
    metrics = metrics_of(done, world)
    assert done.status == "failed" and metrics["citations_valid"] < 1.0 and metrics["bars"]["citations_valid"] is False


def test_with_no_baseline_the_two_relative_bars_cannot_hold_and_the_run_scores_zero(world, monkeypatch):
    monkeypatch.setattr(score, "BASELINE", SUITE_DIR / "no-such-baseline.json")
    metrics = score.metrics_of(
        {"ok": 20, "present": 5, "needing": 5, "citations": 5, "valid": 5, "writes": 0, "cross": 0}, 20, None
    )
    assert metrics["score"] == 0.0 and metrics["bars"]["citations_present"] is False
    assert metrics["bars"]["unconfirmed_writes"] is True


def test_a_recording_that_is_missing_ends_the_run_in_error_and_the_gate_refuses_it(world, tmp_path, capsys):
    done = runner.run_suite(SUITE, tmp_path)  # an empty folder
    assert done.status == "error"
    assert gate.main(SUITE, done.run_id) == 1
    assert "did not finish cleanly" in capsys.readouterr().out


def test_the_floor_the_registration_and_the_bars_are_in_place():
    assert isinstance(runner.SUITES[SUITE], runner.OwnScorer)
    assert gate.floor(SUITE) == 1.0
    readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
    assert MARKER in readme and "not a model quality number" in readme and "Pre-registered bars" in readme
    assert json.loads(json.dumps(MANIFEST)) == MANIFEST
