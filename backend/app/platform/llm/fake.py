"""A provider that replays recorded answers: what CI, the tests and the eval replay use (AI_SYSTEM 2.10).

recorded/<task>.json is a JSON list, consumed in order. A string is the model's text verbatim (script invalid output
with one); anything else is dumped to JSON text (a valid answer). The gateway refuses this provider in production. A
task with no recording raises AIDisabled("no_recording"): the same 503 ai_disabled as no provider at all.

LLM_FAKE_DIR (the demo stack sets it, read at call time) names a directory of recordings that is looked at first, file
by file: <dir>/<task>.json when that file exists, otherwise recorded/<task>.json. A task the directory has no file for
keeps the answer it always had, so one demo's recordings never hide another phase's. replay_from(path) still wins.
"""

import json
import os
from pathlib import Path
from uuid import UUID

from .adapters import Reply
from .failures import AIDisabled

INPUT_TOKENS, OUTPUT_TOKENS = 100, 20  # fixed, so a replay always costs the same

_RECORDED = Path(__file__).parent / "recorded"
_source: Path | None = None
# ponytail: not thread-safe; replays are sequential. Lock it if a test ever runs in threads.
_given: dict[Path, int] = {}


def replay_from(path: Path | None) -> None:
    """Answer every task from this one file, or from recorded/<task>.json when None. Starts the list over."""
    global _source
    _source = path
    _given.clear()


_principals: dict[UUID, Path] = {}  # principal -> directory of <task>.json files (the wind tunnel's candidates)
_taken: dict[tuple[UUID, Path], int] = {}


def replay_for(principal: UUID, directory: Path) -> None:
    """Answer every call made for this principal from <directory>/<task>.json, with counters of its own that start at
    0, however calls of other principals interleave. replay_from does not clear it."""
    _principals[principal] = directory
    for key in [key for key in _taken if key[0] == principal]:
        del _taken[key]


def is_registered(principal: UUID) -> bool:
    return principal in _principals


def forget(principal: UUID) -> None:
    """Return this principal to the ordinary rules."""
    _principals.pop(principal, None)
    for key in [key for key in _taken if key[0] == principal]:
        del _taken[key]


def _path(task: str) -> Path:
    """The file this task is answered from: the scripted one, else the demo directory's file, else recorded/."""
    if _source is not None:
        return _source
    directory = os.environ.get("LLM_FAKE_DIR")
    if directory:
        chosen = Path(directory) / f"{task}.json"
        if chosen.exists():
            return chosen
    return _RECORDED / f"{task}.json"


def _call_for(task: str, principal: UUID) -> Reply:
    path = _principals[principal] / f"{task}.json"
    if not path.exists():
        raise RuntimeError(f"no recorded answer for task {task} in {path.parent.name}")
    answers = json.loads(path.read_text(encoding="utf-8"))
    taken = _taken.get((principal, path), 0)
    if taken >= len(answers):
        raise RuntimeError(f"no recorded answer left in {path.name}")
    _taken[(principal, path)] = taken + 1
    answer = answers[taken]
    return Reply(answer if isinstance(answer, str) else json.dumps(answer), INPUT_TOKENS, OUTPUT_TOKENS)


def call(task: str, principal: UUID | None = None) -> Reply:
    if principal is not None and is_registered(principal):
        return _call_for(task, principal)
    path = _path(task)
    if _source is None and not path.exists():
        # A fresh install runs this provider and holds one recording (ping). A task with none has no answer to give,
        # which is what "no provider" means: AIDisabled at once, not a missing-file error the queue would retry for
        # minutes before the person is told anything.
        raise AIDisabled("no_recording")
    answers = json.loads(path.read_text(encoding="utf-8"))
    taken = _given.get(path, 0)
    if taken >= len(answers):
        raise RuntimeError(f"no recorded answer left in {path.name}")
    _given[path] = taken + 1
    answer = answers[taken]
    return Reply(answer if isinstance(answer, str) else json.dumps(answer), INPUT_TOKENS, OUTPUT_TOKENS)
