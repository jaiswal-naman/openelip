"""A provider that replays recorded answers: what CI, the tests and the eval replay use (AI_SYSTEM 2.10).

recorded/<task>.json is a JSON list, consumed in order. A string is the model's text verbatim (script invalid output
with one); anything else is dumped to JSON text (a valid answer). The gateway refuses this provider in production.
"""

import json
from pathlib import Path

from .adapters import Reply

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


def call(task: str) -> Reply:
    path = _source or _RECORDED / f"{task}.json"
    answers = json.loads(path.read_text(encoding="utf-8"))
    taken = _given.get(path, 0)
    if taken >= len(answers):
        raise RuntimeError(f"no recorded answer left in {path.name}")
    _given[path] = taken + 1
    answer = answers[taken]
    return Reply(answer if isinstance(answer, str) else json.dumps(answer), INPUT_TOKENS, OUTPUT_TOKENS)
