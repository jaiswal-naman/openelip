"""Split a posting into numbered segments: one per non-blank line, with offsets into the original text (AI_SYSTEM 4.3).

Offsets are Python string indices, which count Unicode code points, the unit PostgreSQL substr uses. The web app slices
with Array.from for the same reason, never with UTF-16 indices.
"""

import re
from dataclasses import dataclass

_LINE = re.compile(r"[^\r\n]+")


@dataclass(frozen=True)
class Segment:
    n: int  # 1, 2, 3 ... in reading order
    start: int  # description[start:end] == text
    end: int
    text: str  # the line without its edge whitespace


def segments(description: str) -> list[Segment]:
    found: list[Segment] = []
    for line in _LINE.finditer(description):
        raw = line.group()
        text = raw.strip()
        if text:
            start = line.start() + len(raw) - len(raw.lstrip())
            found.append(Segment(len(found) + 1, start, start + len(text), text))
    return found
