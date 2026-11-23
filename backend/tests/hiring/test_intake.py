"""HIR-02: an employer's role description is read into requirement rows by the task that reads postings, and every way
that can end has a name the API can show (plan 06-02).

The model is the gateway's fake provider answering from the invented recordings of fixtures/intake/ (see the README
there). The gateway is the real one, so each request leaves its ledger row in the test database, which is what the
tests read to prove a blank text costs nothing and a failure is recorded.
"""

import ast
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.hiring import intake
from app.ingest import api as ingest_api
from app.ingest.requirements import task, validate
from app.platform import config, db
from app.platform.llm import ProviderUnavailable, adapters, fake, gateway, registry

FIXTURES = Path(__file__).parent / "fixtures" / "intake"

# Invented. Segments: 1 introduction (accents, a symbol, an emoji outside the Basic Multilingual Plane), 2 heading,
# 3 "3+ years of Python.", 4 "Strong SQL skills.", 5 "Experience with Airflow is a plus.", 6 "We offer free lunch."
DESCRIPTION = (
    "Café Halden Demo ☕ is hiring a data engineer 🚀.\n"
    "What you'll need\n"
    "3+ years of Python.\n"
    "Strong SQL skills.\n"
    "Experience with Airflow is a plus.\n"
    "We offer free lunch."
)


def settings_for(**env: str) -> config.Settings:
    """The real settings, so the database is still the test one, under another provider setup."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    return config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake", **env})


@pytest.fixture
def provider(engine, monkeypatch):
    """The real gateway on the fake provider, with an empty ledger and an employer who saves the description.
    provider(LLM_PROVIDER="anthropic") runs the same gateway under another setup; answers_from("name.json") says which
    recording the fake provider plays. The application's engines are the test's own, so it sees which login wrote."""
    settings = {"value": settings_for()}
    monkeypatch.setattr(config, "get", lambda: settings["value"])
    monkeypatch.setattr(db, "_engines", {})
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE llm_calls, model_registry RESTART IDENTITY CASCADE"))
        employer = conn.execute(
            text("INSERT INTO users (email) VALUES ('intake-employer@example.test') RETURNING id")
        ).scalar_one()

    class Provider:
        def __init__(self) -> None:
            self.employer = employer

        def use(self, **env: str) -> None:
            settings["value"] = settings_for(**env)

        def answers_from(self, name: str | Path) -> None:
            fake.replay_from(FIXTURES / name if isinstance(name, str) else name)  # starts the list over

    yield Provider()
    fake.replay_from(None)
    for application_engine in db._engines.values():
        application_engine.dispose()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE llm_calls, model_registry RESTART IDENTITY CASCADE"))
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": employer})


def extract(engine, description, caller):
    """intake.extract on a connection of its own, as roles.py hands it the request's connection."""
    with engine.connect() as conn:
        return intake.extract(conn, description, caller)


def ledger(engine):
    """The provider requests the gateway recorded, as (task, outcome, person charged, attempt)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT task, outcome::text, user_id, attempt FROM llm_calls ORDER BY id")).all()
    return [tuple(row) for row in rows]


# ---- an answer that works ----------------------------------------


def test_a_valid_answer_becomes_rows_whose_spans_are_code_points_of_the_description(engine, provider):
    provider.answers_from("invented_answer_three.json")
    result = extract(engine, DESCRIPTION, provider.employer)
    assert (result.state, result.dropped) == ("extracted", 0)
    assert [(r.ordinal, r.importance, r.kind, r.min_years) for r in result.rows] == [
        (1, "required", "experience", 3),
        (2, "required", "skill", None),
        (3, "preferred", "skill", None),
    ]
    assert [r.text for r in result.rows] == ["3+ years of Python.", "Strong SQL skills", "Airflow is a plus"]
    assert all(DESCRIPTION[r.start : r.end] == r.text for r in result.rows)
    # The emoji is one code point and two UTF-16 units: a span counted in UTF-16 would start one place too late.
    first = result.rows[0]
    assert first.start == DESCRIPTION.index("3+ years")
    assert first.start != len(DESCRIPTION[: first.start].encode("utf-16-le")) // 2


def test_the_rows_are_the_validators_own_and_the_call_is_recorded_against_the_employer(engine, provider):
    provider.answers_from("invented_answer_three.json")
    result = extract(engine, DESCRIPTION, provider.employer)
    assert all(isinstance(row, ingest_api.RequirementRow) for row in result.rows)
    assert ledger(engine) == [(task.TASK, "ok", provider.employer, 1)]
    with engine.connect() as conn:
        version = conn.execute(text("SELECT prompt_version FROM llm_calls")).scalar_one()
    assert version == task.PROMPT_VERSION  # the prompt and the route are the posting path's, unchanged


def test_the_call_is_recorded_by_the_api_login_alone_because_it_runs_in_the_api_process(engine, provider):
    """The API container holds no worker login: a role saved there failed with OperationalError before this (K1)."""
    provider.answers_from("invented_answer_three.json")
    assert extract(engine, DESCRIPTION, provider.employer).state == "extracted"
    assert set(db._engines) == {"api"}


def test_text_that_states_no_requirement_is_extracted_with_no_rows_and_is_not_called_empty(engine, provider, tmp_path):
    recording = tmp_path / "none.json"
    recording.write_text(json.dumps([{"requirements": []}]), encoding="utf-8")
    provider.answers_from(recording)
    result = extract(engine, "We are a small team that likes cats.", provider.employer)
    assert (result.state, result.rows, result.dropped) == ("extracted", [], 0)
    assert len(ledger(engine)) == 1  # the model was asked and said there was nothing


# ---- the states that are not "extracted" ----------------------------------------


@pytest.mark.parametrize("description", ["", "   ", "\n\t \n"])
def test_a_blank_description_is_empty_and_makes_no_call(engine, provider, description):
    provider.answers_from("invented_answer_three.json")  # a recording is ready, and must stay unused
    result = extract(engine, description, provider.employer)
    assert (result.state, result.rows, result.dropped) == ("empty", [], 0)
    assert ledger(engine) == []
    assert json.loads(fake.call("job.requirements.extract").text)["requirements"]  # still the first answer in the list


def test_with_no_key_the_task_is_ai_disabled_and_nothing_is_called(engine, provider):
    provider.use(LLM_PROVIDER="anthropic")
    result = extract(engine, DESCRIPTION, provider.employer)
    assert (result.state, result.rows, result.dropped) == ("ai_disabled", [], 0)
    assert ledger(engine) == []


def test_a_fake_provider_with_no_recording_is_ai_disabled_too(engine, provider):
    fake.replay_from(None)  # the task has no recording of its own: "no provider" for the gateway
    assert extract(engine, DESCRIPTION, provider.employer).state == "ai_disabled"
    assert ledger(engine) == []


def test_a_provider_that_does_not_answer_is_failed_and_the_ledger_says_so(engine, provider, monkeypatch):
    def timed_out(route, messages, schema):
        raise ProviderUnavailable(timed_out=True)

    monkeypatch.setattr(gateway, "_call", timed_out)
    result = extract(engine, DESCRIPTION, provider.employer)
    assert (result.state, result.rows, result.dropped) == ("failed", [], 0)
    assert ledger(engine) == [(task.TASK, "timeout", provider.employer, 1)]


@pytest.mark.parametrize(
    ("recording", "outcomes"),
    [
        ("invented_answer_unusable.json", ["schema_invalid", "schema_invalid"]),  # the gateway's two attempts
        ("invented_answer_missing_segment_twice.json", ["ok", "ok"]),  # the task's two attempts: no item survives
    ],
)
def test_two_invalid_answers_are_failed_and_never_retried_here(engine, provider, recording, outcomes):
    provider.answers_from(recording)  # two answers: a third request would find none left
    result = extract(engine, DESCRIPTION, provider.employer)
    assert (result.state, result.rows, result.dropped) == ("failed", [], 0)
    assert [outcome for _, outcome, _, _ in ledger(engine)] == outcomes


def test_anything_else_that_goes_wrong_is_not_caught(engine, provider, monkeypatch):
    def broken(route, messages, schema):
        raise RuntimeError("a bug, not a state")

    monkeypatch.setattr(gateway, "_call", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        extract(engine, DESCRIPTION, provider.employer)


# ---- the cap ----------------------------------------


def thirty_five_skills(provider, tmp_path):
    """A description of 35 one-line skills and a recorded answer that cites every one of them."""
    description = "\n".join(f"Skill {n} is required." for n in range(1, 36))
    item = {"quote": None, "importance": "required", "kind": "skill", "min_years": None}
    recording = tmp_path / "thirty_five.json"
    recording.write_text(
        json.dumps([{"requirements": [{"segments": [n], **item} for n in range(1, 36)]}]), encoding="utf-8"
    )
    provider.answers_from(recording)
    return description


def test_more_than_thirty_requirements_are_cut_to_the_first_thirty_and_the_rest_are_counted(engine, provider, tmp_path):
    result = extract(engine, thirty_five_skills(provider, tmp_path), provider.employer)
    assert intake.MAX_REQUIREMENTS == 30
    assert (result.state, len(result.rows), result.dropped) == ("extracted", 30, 5)
    assert [row.ordinal for row in result.rows] == list(range(1, 31))
    assert result.rows[-1].text == "Skill 30 is required."  # the first thirty by position, not the last


def test_the_roles_own_cap_holds_even_if_the_posting_cap_of_the_validator_moves(
    engine, provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(validate, "MAX_REQUIREMENTS", 40)  # the validator now keeps all 35
    result = extract(engine, thirty_five_skills(provider, tmp_path), provider.employer)
    assert (result.state, len(result.rows), result.dropped) == ("extracted", 30, 5)
    assert [row.ordinal for row in result.rows] == list(range(1, 31))


# ---- where it sits ----------------------------------------


def test_the_ingest_api_exports_the_extraction_the_posting_path_runs():
    assert ingest_api.extract_requirements is task.run
    assert {"extract_requirements", "RequirementRow", "ValidationResult"} <= set(ingest_api.__all__)


def test_intake_reaches_the_ingest_module_only_through_its_api_and_never_calls_the_gateway_itself():
    tree = ast.parse(Path(intake.__file__).read_text(encoding="utf-8"))
    imports = [
        (node.module, alias.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names
    ]
    modules = {module for module, _ in imports}
    assert "app.ingest.api" in modules
    assert not {m for m in modules if m and m.startswith("app.ingest.") and m != "app.ingest.api"}
    assert ("app.platform.llm", "complete") not in imports  # no prompt and no route of its own: it asks ingest


# ---- a real route must be proven (B1) ----------------------------------------


def record_run(conn, suite, model, **over):
    """A finished, passed run in eval_runs; the conn fixture rolls it back (the table refuses updates and deletes)."""
    body = {"method": "model", "model": model, "prompt_version": task.PROMPT_VERSION, **over}
    conn.execute(
        text(
            """INSERT INTO eval_runs (suite, git_sha, config, status, metrics, is_gate_passed, finished_at)
               VALUES (CAST(:suite AS eval_suite), 'abc', CAST(:config AS jsonb), 'passed', CAST('{}' AS jsonb), true,
                       now())"""
        ),
        {"suite": suite, "config": json.dumps(body)},
    )


@pytest.fixture
def real_route(provider, monkeypatch):
    """The anthropic route with a dummy key; the provider call is stubbed and a call that happens is listed."""
    provider.use(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-ant-dummy-for-tests")
    calls = []
    answer = fake_answer()

    def called(route, messages, schema):
        calls.append(route.model)
        return adapters.Reply(text=json.dumps(answer), input_tokens=1, output_tokens=1)

    monkeypatch.setattr(gateway, "_call", called)
    return calls


def fake_answer():
    return json.loads((FIXTURES / "invented_answer_three.json").read_text(encoding="utf-8"))[0]


def routed_model():
    return registry.route(task.TASK, config.get()).model


def test_a_real_route_with_no_passed_eval_run_is_ai_disabled_and_the_model_is_never_called(
    engine, conn, provider, real_route
):
    result = intake.extract(conn, DESCRIPTION, provider.employer)
    assert (result.state, result.rows) == ("ai_disabled", [])
    assert real_route == []
    assert ledger(engine) == []


@pytest.mark.parametrize(
    ("suite", "extra"),
    [("requirement_extraction", {"split": "test"}), ("role_intake", {})],
)
def test_a_real_route_with_a_passed_run_for_its_model_and_prompt_is_called(conn, provider, real_route, suite, extra):
    record_run(conn, suite, routed_model(), **extra)
    result = intake.extract(conn, DESCRIPTION, provider.employer)
    assert result.state == "extracted"
    assert real_route == [routed_model()]


@pytest.mark.parametrize(
    ("suite", "wrong"),
    [
        ("requirement_extraction", {"split": "dev"}),
        ("requirement_extraction", {"split": "test", "model": "another-model"}),
        ("requirement_extraction", {"split": "test", "prompt_version": "0123456789ab"}),
        ("requirement_extraction", {"split": "test", "method": "rules"}),
        ("role_intake", {"method": "rules"}),
        ("role_intake", {"model": "another-model"}),
    ],
)
def test_a_run_for_another_split_model_prompt_method_or_suite_proves_nothing(conn, provider, real_route, suite, wrong):
    record_run(conn, suite, wrong.pop("model", routed_model()), **wrong)
    assert intake.extract(conn, DESCRIPTION, provider.employer).state == "ai_disabled"
    assert real_route == []
