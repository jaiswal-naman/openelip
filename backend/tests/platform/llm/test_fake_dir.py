"""LLM_FAKE_DIR (plan 06-07): the demo stack's recordings, chosen per file with recorded/ as the fallback.

These tests point the provider's own recorded/ folder at an empty temporary one, so no file the repository ships can
answer for them: a task with no recording must still raise AIDisabled("no_recording").
"""

import json
from pathlib import Path

import pytest

from app.platform.llm import fake
from app.platform.llm.failures import AIDisabled


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    monkeypatch.setattr(fake, "_RECORDED", shipped)
    monkeypatch.delenv("LLM_FAKE_DIR", raising=False)
    fake.replay_from(None)
    yield shipped
    fake.replay_from(None)


def write(directory: Path, task: str, answers: list) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{task}.json"
    path.write_text(json.dumps(answers), encoding="utf-8")
    return path


def text_of(task: str) -> str:
    return fake.call(task).text


def test_a_file_in_the_directory_answers_in_order(clean, monkeypatch, tmp_path):
    demo = tmp_path / "demo"
    write(demo, "task.a", [{"n": 1}, "raw text"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    assert json.loads(text_of("task.a")) == {"n": 1}
    assert text_of("task.a") == "raw text"
    with pytest.raises(RuntimeError, match="no recorded answer left"):
        fake.call("task.a")


def test_the_directory_wins_over_recorded_for_a_task_it_has(clean, monkeypatch, tmp_path):
    write(clean, "task.a", ["from recorded"])
    demo = tmp_path / "demo"
    write(demo, "task.a", ["from the demo directory"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    assert text_of("task.a") == "from the demo directory"


def test_a_task_the_directory_has_no_file_for_falls_back_to_recorded(clean, monkeypatch, tmp_path):
    write(clean, "task.other", ["from recorded"])
    demo = tmp_path / "demo"
    write(demo, "task.a", ["x"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    assert text_of("task.other") == "from recorded"


def test_each_file_is_consumed_on_its_own(clean, monkeypatch, tmp_path):
    write(clean, "task.other", ["r1", "r2"])
    demo = tmp_path / "demo"
    write(demo, "task.a", ["d1", "d2"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    assert [text_of("task.a"), text_of("task.other"), text_of("task.a"), text_of("task.other")] == [
        "d1",
        "r1",
        "d2",
        "r2",
    ]


def test_replay_from_still_wins_over_the_directory_and_recorded(clean, monkeypatch, tmp_path):
    write(clean, "task.a", ["from recorded"])
    demo = tmp_path / "demo"
    write(demo, "task.a", ["from the demo directory"])
    scripted = write(tmp_path / "scripted", "any", ["scripted"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    fake.replay_from(scripted)
    assert text_of("task.a") == "scripted"


def test_with_the_variable_unset_or_empty_nothing_changes(clean, monkeypatch, tmp_path):
    write(clean, "task.a", ["from recorded"])
    assert text_of("task.a") == "from recorded"
    monkeypatch.setenv("LLM_FAKE_DIR", "")
    fake.replay_from(None)
    assert text_of("task.a") == "from recorded"


def test_a_task_with_no_file_anywhere_is_still_ai_disabled_no_recording(clean, monkeypatch, tmp_path):
    demo = tmp_path / "demo"
    write(demo, "task.a", ["x"])
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    with pytest.raises(AIDisabled) as refused:
        fake.call("task.nobody_recorded")
    assert refused.value.reason == "no_recording"


def test_the_variable_is_read_at_call_time(clean, monkeypatch, tmp_path):
    demo = tmp_path / "demo"
    write(demo, "task.a", ["late"])
    write(clean, "task.a", ["early"])
    assert text_of("task.a") == "early"
    fake.replay_from(None)
    monkeypatch.setenv("LLM_FAKE_DIR", str(demo))
    assert text_of("task.a") == "late"
