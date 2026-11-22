"""The hidden-text slice (AI_SYSTEM 4.1 eval, REC-06): about sixty invented files, one hiding technique each.

evals/resume_extraction/hidden/build_cases.py builds them into a temporary directory for this run; none is committed.
Every hiding file must be flagged with its reason and its hidden text must be gone, except the one known gap (the
invisible text rendering mode), which must keep failing until a detector exists. Legitimate look-alikes must be left
alone, and across all files the visible text wrongly removed stays under 0.5% of lines.

This proves the code path on files built from the same assumptions as the reader. It is not a measured detection rate.
"""

import atexit
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import pytest

from app.profile.resume.reader import read_resume

HIDDEN_DIR = Path(__file__).resolve().parents[3] / "evals" / "resume_extraction" / "hidden"
MAX_WRONGLY_REMOVED = 0.005

_spec = importlib.util.spec_from_file_location("hidden_build_cases", HIDDEN_DIR / "build_cases.py")
assert _spec and _spec.loader
build = importlib.util.module_from_spec(_spec)
sys.modules["hidden_build_cases"] = build
_spec.loader.exec_module(build)

_slice = tempfile.TemporaryDirectory(prefix="hidden-slice-")
atexit.register(_slice.cleanup)
SLICE = Path(_slice.name)
CASES = build.build_cases(SLICE)
HIDING = [c for c in CASES if c.hidden]
LOOK_ALIKES = [c for c in CASES if not c.hidden]

KNOWN_GAP = pytest.mark.xfail(
    strict=True,
    reason="the invisible text rendering mode is not exposed by pdfplumber (AI_SYSTEM 4.1): no detector yet",
)


def read(case):
    return read_resume((SLICE / case.file).read_bytes(), case.mime)


def test_the_slice_has_fifty_hiding_files_in_both_formats_and_exactly_one_known_gap():
    assert len(HIDING) >= 50
    assert sum(c.kind == "pdf" for c in HIDING) >= 30
    assert sum(c.kind == "docx" for c in HIDING) >= 15
    assert [c.technique for c in HIDING if c.known_gap] == [build.KNOWN_GAP]
    assert LOOK_ALIKES, "the slice needs files that look like hiding but are legitimate"


def test_every_file_is_listed_in_the_manifest_and_written():
    manifest = json.loads((SLICE / "manifest.json").read_text(encoding="utf-8"))

    assert [m["file"] for m in manifest] == [c.file for c in CASES]
    assert all((SLICE / c.file).is_file() for c in CASES)
    assert len({c.file for c in CASES}) == len(CASES)


def test_the_slice_is_reproducible_from_its_seed():
    with tempfile.TemporaryDirectory() as other:
        again = build.build_cases(Path(other))

    assert [(c.file, c.hidden, c.visible) for c in again] == [(c.file, c.hidden, c.visible) for c in CASES]


def test_no_slice_file_is_left_in_the_repository():
    assert not [p.name for p in HIDDEN_DIR.iterdir() if p.suffix in {".pdf", ".docx", ".json"}]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.file, marks=[KNOWN_GAP] if c.known_gap else []) for c in HIDING],
)
def test_every_hiding_technique_is_flagged_and_its_text_is_gone(case):
    result = read(case)

    assert result.hidden.found
    assert case.reason in result.hidden.reasons
    assert all(hidden not in result.text for hidden in case.hidden)
    assert result.hidden.chars_removed == sum(len(h.replace(" ", "")) for h in case.hidden)


@pytest.mark.parametrize("case", LOOK_ALIKES, ids=lambda c: c.file)
def test_legitimate_look_alikes_are_not_flagged_and_keep_every_line(case):
    result = read(case)

    assert not result.hidden.found
    assert set(case.visible) <= {line.text for line in result.lines}


def test_visible_text_wrongly_removed_stays_under_half_a_percent_of_lines():
    expected = missing = 0
    for case in CASES:
        kept = {line.text for line in read(case).lines}
        expected += len(case.visible)
        missing += sum(1 for line in case.visible if line not in kept)

    assert expected > 500
    assert missing / expected < MAX_WRONGLY_REMOVED, f"{missing} of {expected} visible lines were removed"
