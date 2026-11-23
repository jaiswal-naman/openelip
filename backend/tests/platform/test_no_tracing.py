"""Third-party tracing cannot be switched on by accident, and LangGraph stays fenced into app.chat (T-05-04, T-05-05).

LangSmith traces would carry prompts and graph state, which hold resume and chat text (ARCHITECTURE 5.9 items 12 to 14),
so the settings refuse to load when any tracing variable is true or a LangSmith or LangChain key exists.
"""

import configparser
import importlib.metadata
import importlib.util
from pathlib import Path

import pytest

from app.platform import config

BACKEND = Path(__file__).resolve().parents[2]
SECRET = "lsv2_pt_0123456789abcdef_this_is_a_secret"
# Written out here, not read from the code under test, so a name dropped from the code fails a test. ARCHITECTURE 5.9
# item 13 names the first and third; the installed langsmith (langsmith.utils.tracing_is_enabled) reads all four.
FLAGS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


def test_the_settings_load_with_no_tracing_variable_set():
    assert config.load({}).tailor_rephrase is False


def test_the_code_refuses_exactly_these_variables():
    assert set(config.TRACING_FLAGS) == set(FLAGS) and set(config.TRACING_KEYS) == set(KEYS)


@pytest.mark.parametrize("name", FLAGS)
@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", "", "  "])
def test_a_tracing_flag_that_is_off_or_empty_loads(name, value):
    config.load({name: value})


@pytest.mark.parametrize("name", FLAGS)
@pytest.mark.parametrize("value", ["true", "TRUE", "True", " true ", "1", "yes", "on"])
def test_a_tracing_flag_that_is_on_stops_the_process_and_names_the_variable(name, value):
    with pytest.raises(config.ConfigError) as refused:
        config.load({name: value})
    assert name in str(refused.value)


@pytest.mark.parametrize("name", KEYS)
def test_a_langsmith_or_langchain_key_stops_the_process_and_is_never_echoed(name):
    with pytest.raises(config.ConfigError) as refused:
        config.load({name: SECRET})
    assert name in str(refused.value) and SECRET not in str(refused.value)


def test_a_key_of_only_spaces_is_unset_as_langsmith_itself_reads_it():
    config.load({"LANGSMITH_API_KEY": "   ", "LANGCHAIN_API_KEY": ""})


def test_every_offending_variable_is_named_at_once():
    with pytest.raises(config.ConfigError) as refused:
        config.load({"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "1", "LANGCHAIN_API_KEY": SECRET})
    named = str(refused.value)
    assert "LANGSMITH_TRACING" in named and "LANGCHAIN_TRACING_V2" in named and "LANGCHAIN_API_KEY" in named
    assert SECRET not in named


def test_a_tracing_flag_nobody_can_read_is_refused_rather_than_guessed():
    with pytest.raises(config.ConfigError) as refused:
        config.load({"LANGSMITH_TRACING": "perhaps"})
    assert "LANGSMITH_TRACING" in str(refused.value) and "perhaps" not in str(refused.value)


def test_tailor_rephrase_is_off_by_default_and_reads_the_usual_spellings():
    assert config.load({}).tailor_rephrase is False
    assert config.load({"TAILOR_REPHRASE": ""}).tailor_rephrase is False
    for word in ("true", "TRUE", "1", "yes", "on"):
        assert config.load({"TAILOR_REPHRASE": word}).tailor_rephrase is True
    for word in ("false", "FALSE", "0", "no", "off"):
        assert config.load({"TAILOR_REPHRASE": word}).tailor_rephrase is False


def test_an_unreadable_tailor_rephrase_stops_the_process_and_is_not_echoed():
    with pytest.raises(config.ConfigError) as refused:
        config.load({"TAILOR_REPHRASE": "sometimes"})
    assert "TAILOR_REPHRASE" in str(refused.value) and "sometimes" not in str(refused.value)


@pytest.fixture
def tracing_on(monkeypatch):
    from app import main, worker  # imported first: app.main builds an app at import, which must see a clean environment

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    config.get.cache_clear()  # the settings are read once per process; read them again with the variable set
    yield main, worker
    monkeypatch.undo()
    config.get.cache_clear()


def test_the_api_refuses_to_start_with_tracing_on(tracing_on):
    main, _ = tracing_on
    with pytest.raises(config.ConfigError, match="LANGSMITH_TRACING"):
        main.create_app()


class WorkerStarted(BaseException):
    """Raised by the stand-in below when the worker gets past its start-up checks. A BaseException on purpose: the
    worker's loop catches every Exception and carries on, so anything milder would let a worker with no guard run
    forever against the database (the loop is infinite) instead of failing the test."""


def _worker_started(*args, **kwargs):
    raise WorkerStarted


def test_the_worker_refuses_to_start_with_tracing_on(tracing_on, monkeypatch):
    _, worker = tracing_on
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)  # do not leave a SIGTERM handler behind
    monkeypatch.setattr(worker.log, "event", _worker_started)  # the first thing it does once the settings are read
    with pytest.raises(config.ConfigError, match="LANGSMITH_TRACING"):
        worker.main()


def test_langgraph_is_pinned_and_no_langchain_provider_package_is_installed():
    assert importlib.metadata.version("langgraph") == "1.2.24"
    assert importlib.metadata.version("reportlab") == "5.1.0"
    for package in ("langchain_anthropic", "langchain_openai", "langchain_community", "langgraph_api", "langgraph_cli"):
        assert importlib.util.find_spec(package) is None, f"{package} must not be installed (ARCHITECTURE 5.9 item 12)"


def _contracts() -> dict[str, dict[str, list[str]]]:
    """Every contract of backend/.importlinter as {name: {option: [lines]}}."""
    parser = configparser.ConfigParser()
    parser.read(BACKEND / ".importlinter")

    def lines(value: str) -> list[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]

    return {
        section: {key: lines(value) for key, value in parser[section].items()}
        for section in parser.sections()
        if section.startswith("importlinter:contract:")
    }


def _forbidding(module: str) -> dict[str, list[str]]:
    found = [c for c in _contracts().values() if c["type"] == ["forbidden"] and module in c["forbidden_modules"]]
    assert len(found) == 1, f"expected exactly one forbidden contract naming {module}"
    return found[0]


def test_langgraph_is_forbidden_everywhere_except_the_four_modules_that_architecture_names():
    contract = _forbidding("langgraph")
    assert contract["source_modules"] == ["app"] and contract["forbidden_modules"] == ["langgraph"]
    # an importer that does not exist yet is not an error, so the later plans need no edit here
    assert contract["unmatched_ignore_imports_alerting"] == ["none"]
    for importer in ("app.chat", "app.guardian.runtime", "app.campaigns", "app.twin"):
        assert f"{importer} -> langgraph" in contract["ignore_imports"]
        assert f"{importer}.** -> langgraph" in contract["ignore_imports"]
    assert len(contract["ignore_imports"]) == 8  # nothing else is let through


def test_langchain_core_and_langgraph_sdk_are_forbidden_to_all_of_app():
    contract = _forbidding("langchain_core")
    assert contract["source_modules"] == ["app"]
    assert sorted(contract["forbidden_modules"]) == ["langchain_core", "langgraph_sdk"]
    assert "ignore_imports" not in contract
