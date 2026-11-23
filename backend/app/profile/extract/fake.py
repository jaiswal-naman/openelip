"""Recorded answers for the three resume.facts tasks, replayed through the gateway's fake provider (AI_SYSTEM 12.8).

The gateway's fake provider answers in call order from one JSON list and never sees the request. A recording here is
keyed by the hash of the request it answers instead, so a changed prompt, mask or resume makes the recording stale and
the replay stops, rather than quietly replaying answers to a different question. replay_file turns the recordings for a
run's requests into the ordered list the provider reads. The recordings are made by a script, not by a model, and prove
only that the code path works (invariant 8).
"""

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.profile.resume.reader import HiddenReport, Line, ReadResult


class RecordingStale(Exception):
    """A request has no recording. The message names the task only, never the request.
    Run evals/resume_extraction/facts/record_fake.py again."""


def request_hash(task: str, messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps([task, messages], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load(path: Path) -> dict[str, list[Any]]:
    """A recordings file (one JSON object per line: task, request, answers) as request hash -> answers. The answers are
    one entry, or two when the first is invalid output (a string) and the gateway asks again."""
    recordings: dict[str, list[Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["request"] in recordings:
            raise ValueError(f"{path.name} records one request twice")
        recordings[entry["request"]] = entry["answers"]
    return recordings


def replay_file(
    requests: Iterable[tuple[str, list[dict[str, str]]]], recordings: dict[str, list[Any]], path: Path
) -> Path:
    """Write the answers to these requests, in order, where the gateway's fake provider reads them (replay_from)."""
    answers: list[Any] = []
    for task, messages in requests:
        try:
            answers += recordings[request_hash(task, messages)]
        except KeyError:
            raise RecordingStale(task) from None
    path.write_text(json.dumps(answers), encoding="utf-8")
    return path


def result_from_lines(lines: list[str]) -> ReadResult:
    """What the reader returns for lines that are already clean: one per row, joined by newlines. For the gold set
    and for tests."""
    numbered: list[Line] = []
    start = 0
    for number, text in enumerate(lines, 1):
        numbered.append(Line(n=number, page=1, start=start, end=start + len(text), text=text))
        start += len(text) + 1
    return ReadResult(
        lines=numbered,
        text="\n".join(lines),
        hidden=HiddenReport(found=False, chars_removed=0, samples=[], reasons=[]),
        scanned=False,
    )
