"""EVL-01 (plan 05-06, task 3): resume extraction, requirement extraction, evidence faithfulness and tailoring integrity
run in the CI eval gate on every pull request, each with a floor equal to its committed baseline, and a Phase 5
prompt edit fails CI until its recordings are made again.

The checks read files (.github/workflows/ci.yml, evals/thresholds.toml, the baselines and manifests); only the last one
opens a database, to run the gate on a synthetic run.
"""

import json
import tomllib

import pytest
import yaml
from sqlalchemy import text

from app.evals import EVALS, gate, runner
from app.platform.llm import embed  # noqa: F401  (imported so a missing optional dependency shows here, not in a suite)
from tests.evals import p5_env

ROOT = EVALS.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
THRESHOLDS = EVALS / "thresholds.toml"
APP = ROOT / "backend" / "app"

# the four suites EVL-01 names, with where each keeps its gold set, its recordings and its committed baseline score
EVL01 = {
    "resume_extraction": {
        "gold": "resume_extraction/facts/gold.jsonl",
        "recorded": "resume_extraction/facts/recordings.jsonl",
    },
    "requirement_extraction": {
        "gold": "requirement_extraction/gold.jsonl",
        "recorded": "requirement_extraction/recordings.jsonl",
    },
    "evidence_faithfulness": {
        "gold": "evidence_faithfulness/gold.jsonl",
        "recorded": "evidence_faithfulness/recorded.json",
    },
    "tailoring_integrity": {"gold": "tailoring_integrity/gold.jsonl", "recorded": "tailoring_integrity/recordings"},
}
PHASE_5_SAFETY = ("chat", "record_proposals", "injection")  # gated beside them, with a baseline of their own
PHASE_5_MANIFESTS = ("tailoring_integrity", "chat", "record_proposals", "injection")
PROMPTS = {  # every task a Phase 5 recording answers, and the prompt file whose hash is its version
    "tailor.propose": APP / "tailor" / "prompts" / "tailor_propose.md",
    "verify.entail": APP / "tailor" / "prompts" / "verify_entail.md",
    "chat.turn": APP / "chat" / "prompts" / "chat_turn.md",
    "record.propose": APP / "chat" / "prompts" / "record_propose.md",
    "writer.packet": APP / "apply" / "prompts" / "writer_packet.md",
    "match.evidence.score": APP / "match" / "prompts" / "evidence_score.md",
}


def baseline_score(suite: str) -> float:
    """The score the suite's committed baseline holds. evidence_faithfulness scores 1.0 or 0.0 on bars written before
    the run and keeps no baseline file: its baseline is the 1.0 its committed recording must reach."""
    if suite == "resume_extraction":
        return float(json.loads((EVALS / "resume_extraction/facts/baseline.json").read_text(encoding="utf-8"))["score"])
    if suite == "requirement_extraction":
        runs = json.loads((EVALS / "requirement_extraction/baseline.json").read_text(encoding="utf-8"))["runs"]
        return float(runs["replay"]["metrics"]["score"])
    if suite == "evidence_faithfulness":
        return 1.0
    return float(json.loads((EVALS / suite / "baseline.json").read_text(encoding="utf-8"))["score"])


def floors() -> dict[str, float]:
    with THRESHOLDS.open("rb") as handle:
        return {name: float(entry["min_score"]) for name, entry in tomllib.load(handle).items()}


def matrix(workflow_text: str) -> list[str]:
    """The suites of the eval-gate job's matrix, parsed as YAML."""
    return list(yaml.safe_load(workflow_text)["jobs"]["eval-gate"]["strategy"]["matrix"]["suite"])


def missing_from(workflow_text: str, suites) -> list[str]:
    listed = matrix(workflow_text)
    return [suite for suite in suites if suite not in listed]


def stale(manifest: dict, versions: dict[str, str]) -> list[tuple[str, str]]:
    """The (file, task) entries whose prompt_version is not the current hash of the task's prompt file."""
    return [(f, t) for f, tasks in manifest.items() for t, version in tasks.items() if version != versions[t]]


def current_versions() -> dict[str, str]:
    return {task: p5_env.prompt_hash(path) for task, path in PROMPTS.items()}


# ---- the four suites ----------------------------------------


@pytest.mark.parametrize("suite", EVL01)
def test_each_evl01_suite_is_registered_has_a_gold_set_and_recordings_a_floor_at_its_baseline_and_a_place_in_the_matrix(
    suite,
):
    # requirement_extraction is scored by its own script (the matrix runs `score.py --record`), not by runner.SUITES
    assert suite in runner.SUITES or (EVALS / suite / "score.py").is_file()
    assert (EVALS / EVL01[suite]["gold"]).is_file() and (EVALS / EVL01[suite]["recorded"]).exists()
    assert floors()[suite] == baseline_score(suite), suite
    assert suite in matrix(CI.read_text(encoding="utf-8"))


def test_the_three_phase_5_safety_suites_are_registered_floored_at_their_baselines_and_in_the_matrix():
    for suite in PHASE_5_SAFETY:
        assert suite in runner.SUITES and floors()[suite] == baseline_score(suite) == 1.0
        assert suite in matrix(CI.read_text(encoding="utf-8"))


def test_every_suite_in_the_matrix_has_a_floor_and_every_registered_suite_is_in_the_matrix():
    listed = set(matrix(CI.read_text(encoding="utf-8")))
    assert listed <= set(floors()), "a suite with no floor fails its gate"
    assert set(runner.SUITES) <= listed, "a registered suite the pull request never runs"


def test_the_matrix_parsed_as_yaml_agrees_with_the_suite_line_of_the_file():
    workflow = CI.read_text(encoding="utf-8")
    line = next(row for row in workflow.splitlines() if row.strip().startswith("suite: ["))
    listed = [name.strip() for name in line.split("[", 1)[1].split("]", 1)[0].split(",")]
    assert listed == matrix(workflow)


@pytest.mark.parametrize("suite", [*EVL01, *PHASE_5_SAFETY])
def test_removing_a_suite_from_the_matrix_of_a_copy_of_the_file_makes_the_check_fail(suite, tmp_path):
    workflow = CI.read_text(encoding="utf-8")
    assert missing_from(workflow, [suite]) == []
    copy = workflow.replace(f", {suite}", "").replace(f"{suite}, ", "")
    assert copy != workflow and missing_from(copy, [suite]) == [suite]
    assert missing_from(copy, [*EVL01]) == ([suite] if suite in EVL01 else [])


def test_the_eval_job_names_the_database_evals_test_everywhere_and_gives_the_api_and_match_roles_a_login():
    job = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["eval-gate"]
    assert job["services"]["db"]["env"]["POSTGRES_DB"].split()[0] == "evals_test"
    urls = {name: value for name, value in job["env"].items() if name.startswith("DATABASE_URL_")}
    assert set(urls) == {"DATABASE_URL_MIGRATE", "DATABASE_URL_API", "DATABASE_URL_WORKER", "DATABASE_URL_MATCH"}
    assert all(value.endswith("/evals_test") for value in urls.values()), urls
    assert urls["DATABASE_URL_API"].startswith("postgresql://app_api:app_api_local_only@")
    assert urls["DATABASE_URL_MATCH"].startswith("postgresql://app_match:app_match_local_only@")
    assert job["env"]["LLM_PROVIDER"] == "fake"


# ---- stale recordings ----------------------------------------


@pytest.mark.parametrize("suite", PHASE_5_MANIFESTS)
def test_every_task_listed_under_every_file_of_a_phase_5_manifest_has_the_current_prompt_hash(suite):
    folder = EVALS / suite / "recordings"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    versions = current_versions()
    assert manifest and all(tasks and set(tasks) <= set(PROMPTS) for tasks in manifest.values()), suite
    assert stale(manifest, versions) == [], f"{suite}: re-record the files that name an old prompt version"
    for name in manifest:
        assert (folder / name).is_file(), (suite, name)
    on_disk = {p.name for p in folder.glob("*.json") if p.name != "manifest.json"}
    assert on_disk == set(manifest), suite


def test_every_prompt_file_a_manifest_names_exists_and_its_hash_is_the_twelve_characters_the_modules_use():
    from app.apply import packet
    from app.chat import loop, proposals
    from app.match import evidence
    from app.tailor import pipeline

    versions = current_versions()
    assert versions == {
        "tailor.propose": pipeline.PROPOSE_VERSION,
        "verify.entail": pipeline.ENTAIL_VERSION,
        "chat.turn": loop.PROMPT_VERSION,
        "record.propose": proposals.PROMPT_VERSION,
        "writer.packet": packet.PROMPT_VERSION,
        "match.evidence.score": evidence.PROMPT_VERSION,
    }
    assert all(len(v) == 12 for v in versions.values())


@pytest.mark.parametrize("task", PROMPTS)
def test_a_copied_manifest_with_a_changed_prompt_version_fails_the_check_for_exactly_that_task(task):
    versions = current_versions()
    for suite in PHASE_5_MANIFESTS:
        manifest = json.loads((EVALS / suite / "recordings" / "manifest.json").read_text(encoding="utf-8"))
        answering = [name for name, tasks in manifest.items() if task in tasks]
        if not answering:
            continue
        changed = {name: dict(tasks) for name, tasks in manifest.items()}
        changed[answering[0]][task] = "0" * 12  # as if the prompt had been edited and nothing re-recorded
        assert stale(changed, versions) == [(answering[0], task)]
        assert stale(manifest, versions) == []


def test_editing_a_prompt_file_changes_its_hash_so_the_recordings_that_named_the_old_one_go_stale(tmp_path):
    original = PROMPTS["record.propose"]
    edited = tmp_path / "record_propose.md"
    edited.write_text(original.read_text(encoding="utf-8") + "\nOne more sentence.\n", encoding="utf-8")
    assert p5_env.prompt_hash(edited) != p5_env.prompt_hash(original)
    versions = {**current_versions(), "record.propose": p5_env.prompt_hash(edited)}
    for suite in ("chat", "record_proposals", "injection"):
        manifest = json.loads((EVALS / suite / "recordings" / "manifest.json").read_text(encoding="utf-8"))
        assert any(task == "record.propose" for _, task in stale(manifest, versions)), suite


# ---- the older recorded suites (05-FIXES B5) ----------------------------------------

OLDER_MANIFEST = EVALS / "older_recordings_manifest.json"


def older_current_versions() -> dict[str, str]:
    """The current version of every prompt the three older suites' recordings answer, from the code that sends it."""
    from app.ingest.requirements import task as requirement_task
    from app.match import evidence
    from app.profile.extract import workflow

    return {
        **workflow.prompt_versions(),
        requirement_task.TASK: requirement_task.PROMPT_VERSION,
        evidence.TASK: evidence.PROMPT_VERSION,
    }


def older_stale(manifest: dict, versions: dict[str, str]) -> list[tuple[str, str]]:
    """The (suite, task) entries whose stored version is not the current one (an entry for a task nobody sends too)."""
    return [
        (suite, task)
        for suite, tasks in manifest.items()
        if suite != "note"
        for task, version in tasks.items()
        if versions.get(task) != version
    ]


def test_the_stored_prompt_versions_of_the_three_older_suites_are_the_current_prompt_hashes():
    manifest = json.loads(OLDER_MANIFEST.read_text(encoding="utf-8"))
    assert {name for name in manifest if name != "note"} == {
        "resume_extraction",
        "requirement_extraction",
        "evidence_faithfulness",
    }
    assert older_stale(manifest, older_current_versions()) == [], "re-record the suite that names an old prompt version"
    # every task the current code sends is listed: a new prompt cannot slip in without a recording
    assert {t for suite, tasks in manifest.items() if suite != "note" for t in tasks} == set(older_current_versions())
    baseline = json.loads((EVALS / "requirement_extraction/baseline.json").read_text(encoding="utf-8"))
    assert (
        baseline["runs"]["replay"]["config"]["prompt_version"]
        == manifest["requirement_extraction"]["job.requirements.extract"]
    )


@pytest.mark.parametrize("task", ["resume.facts.roles", "job.requirements.extract", "match.evidence.score"])
def test_an_edited_prompt_of_an_older_suite_makes_its_stored_version_stale(task):
    manifest = json.loads(OLDER_MANIFEST.read_text(encoding="utf-8"))
    edited = {**older_current_versions(), task: "0" * 12}  # as if the prompt file had changed and nothing re-recorded
    suites = [suite for suite, tasks in manifest.items() if suite != "note" and task in tasks]
    assert suites and [t for _, t in older_stale(manifest, edited)] == [task] * len(suites)


# ---- the gate fails below the floor ----------------------------------------


def synthetic_run(engine, suite: str, score: float):
    metrics = json.dumps({"n": 10, "passed": 10, "score": score})
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO eval_runs (suite, git_sha, status, metrics, finished_at) VALUES "
                "(CAST(:suite AS eval_suite), 'test', 'passed', CAST(:metrics AS jsonb), now()) RETURNING id"
            ),
            {"suite": suite, "metrics": metrics},
        ).scalar_one()


@pytest.mark.parametrize("suite", EVL01)
def test_the_gate_fails_a_run_whose_score_is_below_the_floor_of_each_evl01_suite_and_passes_one_at_the_floor(
    engine, suite, capsys
):
    floor = floors()[suite]
    below = synthetic_run(engine, suite, floor - 0.01)
    assert gate.main(suite, below) == 1
    assert "FAIL" in capsys.readouterr().out
    at_floor = synthetic_run(engine, suite, floor)
    assert gate.main(suite, at_floor) == 0
    assert "PASS" in capsys.readouterr().out
