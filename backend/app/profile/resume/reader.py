"""Resume reader (AI_SYSTEM 4.1; requirements REC-01 and REC-06).

read_resume(data, mime) turns a PDF or DOCX into numbered lines of visible text and a report of the hidden text it left
out. Hidden characters are dropped before any line is built, so every offset indexes the cleaned text, and they are
never used (threat T-3-05). The report's samples are for showing the candidate what was removed: nothing here logs
text (T-3-08). There is no OCR: a file with almost no text is reported as scanned and returns no lines.

PDF: a character is hidden when it is under MIN_FONT_PT, lies outside the visible page box, is covered by a later,
different character, or its fill is too close in brightness to the topmost filled shape under it. White text on a dark
sidebar has contrast and stays. The invisible text rendering mode is not exposed by pdfplumber, so text in that mode
reads as visible: a known gap, kept as an expected-failure fixture in evals/resume_extraction/hidden.
DOCX: a run is hidden when it is marked hidden, tiny, or coloured like the page, with the style chain resolved first
(python-docx reports None for "inherit").
"""

import io
import re
import zipfile
from dataclasses import dataclass
from operator import itemgetter
from typing import Any

import pdfplumber
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from pdfminer.pdfdocument import PDFEncryptionError
from pdfplumber.utils import cluster_objects
from pdfplumber.utils.exceptions import PdfminerException

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# ponytail: starting values from AI_SYSTEM 4.1. They are assumptions to tune on the resume gold set, not measurements.
MIN_FONT_PT = 4
MIN_CONTRAST = 1.5
MIN_CHARS_PER_PAGE = 200
OVERLAP_RATIO = 0.5  # of the smaller character box, for "covered by another character"

# ponytail: layout heuristics for one gutter, so two columns. A page with three columns is read row by row.
ROW_TOLERANCE_PT = 4  # characters whose vertical centres differ by less share a row (a superscript sits off its line)
WORD_GAP_EM = 0.15  # a gap wider than this fraction of the font size is a space
MIN_GUTTER_PT = 10
MIN_COLUMN_SHARE = 0.15  # each side of a gutter holds at least this share of the characters ...
MIN_COLUMN_ROWS = 3  # ... and this many rows, or it is a right-aligned date column, not a column
FULL_WIDTH_SLACK = 0.25  # share of rows that may run across a gutter (a banner, a footer) before it stops being one

# Limits against a hostile file (T-3-06). The caller also caps the upload at 10 MB.
MAX_PAGES = 20
MAX_CHARS_PER_PAGE = 30_000
MAX_FILLED_SHAPES = 500  # past this many, the colour check on that page is skipped rather than made slow
MAX_DOCX_PARTS = 2_000
MAX_DOCX_UNZIPPED_BYTES = 50 * 1024 * 1024

MAX_SAMPLES = 3
SAMPLE_CHARS = 40
# The fixed vocabulary of reasons, in the order a report lists them.
REASONS = ("tiny_font", "off_page", "overlapped", "text_colour_matches_background", "docx_hidden_run")

_WHITE = (1.0, 1.0, 1.0)
_LIGATURES = str.maketrans(
    {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}
)
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")  # a password-protected DOCX is an OLE file, not a zip
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"  # a duplicate of a text box


@dataclass(frozen=True)
class Line:
    n: int  # 1-based, continuing across pages
    page: int  # 1-based; a DOCX has no fixed pages, so its lines say 1
    start: int  # text[start:end] == text
    end: int
    text: str


@dataclass(frozen=True)
class HiddenReport:
    found: bool
    chars_removed: int  # visible-looking characters dropped, spaces not counted
    samples: list[str]  # at most 3, 40 characters each, to show the candidate; never logged
    reasons: list[str]  # from REASONS


@dataclass(frozen=True)
class ReadResult:
    lines: list[Line]
    text: str  # the lines joined by "\n"
    hidden: HiddenReport
    scanned: bool  # too little text for a text file; lines and text are then empty


class ResumeUnreadable(Exception):
    """The file cannot be read. `code` is `corrupt`, `encrypted` or `scanned`; the message is the code, never content.

    read_resume reports a scanned file through ReadResult.scanned; a caller that treats it as a failure raises this.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Hidden:
    """What was removed: how much, a few samples to show the candidate, and why."""

    def __init__(self) -> None:
        self.chars = 0
        self.samples: list[str] = []
        self.reasons: set[str] = set()

    def add(self, text: str, reasons: set[str]) -> None:
        text = " ".join(text.split())
        removed = sum(1 for ch in text if not ch.isspace())
        if not removed:
            return
        self.chars += removed
        self.reasons |= reasons
        if len(self.samples) < MAX_SAMPLES and text[:SAMPLE_CHARS] not in self.samples:
            self.samples.append(text[:SAMPLE_CHARS])

    def report(self) -> HiddenReport:
        return HiddenReport(self.chars > 0, self.chars, self.samples, [r for r in REASONS if r in self.reasons])


def read_resume(data: bytes, mime: str) -> ReadResult:
    if mime == PDF_MIME:
        pages, hidden = _read_pdf(data)
    elif mime == DOCX_MIME:
        pages, hidden = _read_docx(data)
    else:
        raise ValueError("read_resume reads PDF and DOCX only")  # a caller bug: the documents table forbids others
    report = hidden.report()
    visible = sum(len(t.replace(" ", "")) for texts in pages for t in texts)
    if visible < MIN_CHARS_PER_PAGE * len(pages):
        return ReadResult([], "", report, scanned=True)
    lines: list[Line] = []
    offset = 0
    for page_number, texts in enumerate(pages, start=1):
        for text in texts:
            lines.append(Line(len(lines) + 1, page_number, offset, offset + len(text), text))
            offset += len(text) + 1  # the "\n" between lines
    return ReadResult(lines, "\n".join(line.text for line in lines), report, scanned=False)


# --- colour --------------------------------------------------------------------------------------------------------


def _rgb(color: Any) -> tuple[float, float, float] | None:
    """A PDF fill as RGB in 0..1. An unset fill is black; a pattern, or anything not a plain colour, is None."""
    if color is None or color == (None,):
        return (0.0, 0.0, 0.0)
    if not isinstance(color, tuple) or not all(isinstance(v, int | float) for v in color):
        return None
    v = [min(max(float(x), 0.0), 1.0) for x in color]
    if len(v) == 1:
        return (v[0], v[0], v[0])
    if len(v) == 3:
        return (v[0], v[1], v[2])
    if len(v) == 4:  # CMYK
        return ((1 - v[0]) * (1 - v[3]), (1 - v[1]) * (1 - v[3]), (1 - v[2]) * (1 - v[3]))
    return None


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = ((c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """The WCAG contrast ratio: 1 for equal brightness, 21 for black on white."""
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _clean(text: str) -> str:
    """One line of text: ligatures expanded, control and zero-width characters gone, white space collapsed."""
    text = text.translate(_LIGATURES)
    return " ".join("".join(ch for ch in text if ch.isprintable() or ch.isspace()).split())


# --- PDF -----------------------------------------------------------------------------------------------------------


def _read_pdf(data: bytes) -> tuple[list[list[str]], _Hidden]:
    hidden = _Hidden()
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if not 0 < len(pdf.pages) <= MAX_PAGES:
                raise ResumeUnreadable("corrupt")
            pages = []
            for page in pdf.pages:
                pages.append(_pdf_page_lines(page, hidden))
                page.flush_cache()
    except ResumeUnreadable:
        raise
    except PdfminerException as error:
        inner = error.args[0] if error.args else None
        raise ResumeUnreadable("encrypted" if isinstance(inner, PDFEncryptionError) else "corrupt") from None
    except Exception:  # untrusted bytes in a third-party parser: whatever it raises, the file is unreadable
        raise ResumeUnreadable("corrupt") from None
    return pages, hidden


def _visible_box(page: Any) -> tuple[float, float, float, float]:
    crop, media = page.cropbox, page.mediabox
    return (max(crop[0], media[0]), max(crop[1], media[1]), min(crop[2], media[2]), min(crop[3], media[3]))


def _background(
    shapes: list[dict[str, Any]], cx: float, cy: float, char: dict[str, Any]
) -> tuple[float, float, float] | None:
    """The fill under a character: the topmost painted shape that holds its centre. White for the bare page; None when
    it cannot be known (an image, a pattern, or too many shapes to check)."""
    if len(shapes) > MAX_FILLED_SHAPES:
        return None
    least = char["height"] / 2  # a hairline rule is not a background
    for shape in reversed(shapes):
        if shape["x0"] <= cx <= shape["x1"] and shape["top"] <= cy <= shape["bottom"]:
            if shape["height"] >= least and shape["width"] >= least:
                return None if shape["object_type"] == "image" else _rgb(shape.get("non_stroking_color"))
    return _WHITE


def _font_size(char: dict[str, Any]) -> float:
    """The font size: the character's box across its baseline. pdfplumber's own `size` is the glyph's advance for
    sideways text, which would make every narrow letter of a rotated line look tiny."""
    return char["height"] if char["upright"] else char["width"]


def _char_reasons(
    char: dict[str, Any], box: tuple[float, float, float, float], shapes: list[dict[str, Any]]
) -> set[str]:
    reasons: set[str] = set()
    cx, cy = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
    if not (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]):
        reasons.add("off_page")
    if _font_size(char) < MIN_FONT_PT:
        reasons.add("tiny_font")
    fill, behind = _rgb(char.get("non_stroking_color")), _background(shapes, cx, cy, char)
    if fill is not None and behind is not None and _contrast(fill, behind) < MIN_CONTRAST:
        reasons.add("text_colour_matches_background")
    return reasons


def _overlaps(chars: list[dict[str, Any]]) -> tuple[set[int], set[int]]:
    """Indices into `chars` (in paint order) of characters covered by a later, different one, and of exact duplicates
    (a fake bold drawn twice) whose earlier copy is dropped without a report."""
    covered: set[int] = set()
    duplicates: set[int] = set()
    active: list[int] = []
    for i in sorted(range(len(chars)), key=lambda k: chars[k]["x0"]):
        c = chars[i]
        active = [j for j in active if chars[j]["x1"] > c["x0"]]
        for j in active:
            d = chars[j]
            if d["top"] >= c["bottom"] or d["bottom"] <= c["top"]:  # another row: the common case on a dense page
                continue
            smaller = min(c["width"] * c["height"], d["width"] * d["height"])
            width = min(c["x1"], d["x1"]) - max(c["x0"], d["x0"])
            height = min(c["bottom"], d["bottom"]) - max(c["top"], d["top"])
            if smaller <= 0 or width * height < OVERLAP_RATIO * smaller:
                continue
            if c["text"] == d["text"]:
                duplicates.add(min(i, j))
            elif c["text"].isalnum() and d["text"].isalnum():
                covered.add(min(i, j))  # the earlier one is under the later one
        active.append(i)
    return covered, duplicates


def _pdf_page_lines(page: Any, hidden: _Hidden) -> list[str]:
    box = _visible_box(page)
    chars: list[dict[str, Any]] = []
    why: list[set[str]] = []
    shapes: list[dict[str, Any]] = []  # filled shapes painted so far; they sit under the characters painted after them
    for obj in page.iter_layout_objects(list(page.layout)):
        kind = obj["object_type"]
        if kind == "char":
            chars.append(obj)
            why.append(_char_reasons(obj, box, shapes))
        elif kind == "image" or (kind in ("rect", "curve") and obj.get("fill")):
            shapes.append(obj)
    if len(chars) > MAX_CHARS_PER_PAGE:
        raise ResumeUnreadable("corrupt")

    # Spaces are not laid out: a word gap is read from the positions of the letters on either side. (A hidden space
    # under a visible letter would otherwise be sorted into the word.)
    live = [i for i, reasons in enumerate(why) if not reasons and not chars[i]["text"].isspace()]
    covered, duplicates = _overlaps([chars[i] for i in live])
    for k in covered:
        why[live[k]].add("overlapped")
    dropped = {live[k] for k in duplicates}

    # One hidden string per row of hidden characters, so separate lines are separate samples.
    reasons_of = {id(c): reasons for c, reasons in zip(chars, why, strict=True)}
    for row in _rows([c for c, reasons in zip(chars, why, strict=True) if reasons and not c["text"].isspace()]):
        hidden.add(_row_text(row), set().union(*(reasons_of[id(c)] for c in row)))

    return _page_lines([chars[i] for i in live if i not in dropped and not why[i]])


def _middle(char: dict[str, Any]) -> float:
    return float((char["top"] + char["bottom"]) / 2)


def _rows(chars: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Upright text: rows from top to bottom, each from left to right. Sideways text (a rotated title, or a page shown
    on its side): each line is a column of characters, read the way it runs, the lines in the order they stack."""
    upright = [c for c in chars if c["upright"]]
    rows = [sorted(row, key=itemgetter("x0")) for row in cluster_objects(upright, _middle, ROW_TOLERANCE_PT)]
    sideways = [c for c in chars if not c["upright"]]
    for reads_down in (True, False):  # the text matrix says which way a line runs
        turned = [c for c in sideways if (c["matrix"][1] < 0) == reads_down]
        columns = cluster_objects(turned, "x0", ROW_TOLERANCE_PT)
        columns.sort(key=lambda column: column[0]["x0"], reverse=reads_down)  # lines stack leftward when reading down
        rows += [sorted(column, key=itemgetter("top"), reverse=not reads_down) for column in columns]
    return rows


def _gap(previous: dict[str, Any], c: dict[str, Any]) -> float:
    """The empty space between two neighbours of a row, measured along the way the row runs."""
    if c["upright"]:
        return float(c["x0"] - previous["x1"])
    return float(max(c["top"] - previous["bottom"], previous["top"] - c["bottom"]))


def _row_text(row: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    previous = None
    for c in row:
        if previous is not None and _gap(previous, c) > WORD_GAP_EM * _font_size(previous):
            parts.append(" ")
        parts.append(c["text"])
        previous = c
    return _clean("".join(parts))


def _segments(row: list[dict[str, Any]]) -> list[list[float]]:
    """A row as [x0, x1] runs of text, split wherever the gap is wide enough to be a column gutter."""
    runs: list[list[float]] = []
    for c in row:
        if runs and c["x0"] - runs[-1][1] < MIN_GUTTER_PT:
            runs[-1][1] = max(runs[-1][1], c["x1"])
        else:
            runs.append([c["x0"], c["x1"]])
    return runs


def _crosses(row: list[dict[str, Any]], x: float) -> bool:
    return any(start < x < end for start, end in _segments(row))


def _split(rows: list[list[dict[str, Any]]]) -> float | None:
    """The x position of a column gutter, or None. A position is a gutter when only a few full-width rows (a name
    banner) run across it and each side holds a real share of the text, which a right-aligned date column does not."""
    spans = [_segments(row) for row in rows]
    total = sum(map(len, rows))
    slack = max(1, int(FULL_WIDTH_SLACK * len(rows)))
    best, best_size = None, 0
    for x in sorted({(a[1] + b[0]) / 2 for segs in spans for a, b in zip(segs, segs[1:])}):
        if sum(any(start < x < end for start, end in segs) for segs in spans) > slack:
            continue
        left = [sum((c["x0"] + c["x1"]) / 2 < x for c in row) for row in rows]
        sides = (left, [len(row) - n for row, n in zip(rows, left, strict=True)])
        size = min(sum(side) for side in sides)
        if size > best_size and all(
            sum(side) >= MIN_COLUMN_SHARE * total and sum(1 for n in side if n) >= MIN_COLUMN_ROWS for side in sides
        ):
            best, best_size = x, size
    return best


def _page_lines(chars: list[dict[str, Any]]) -> list[str]:
    """Reading order for one page: rows top to bottom, or, when there are two columns, the left column fully before the
    right between full-width rows."""
    rows = _rows(chars)
    x = _split(rows) if len(rows) >= 2 * MIN_COLUMN_ROWS else None
    if x is None:
        return [t for t in map(_row_text, rows) if t]
    lines: list[str] = []
    left: list[str] = []
    right: list[str] = []
    for row in rows:
        if _crosses(row, x):
            lines += left + right
            left, right = [], []
            lines.append(_row_text(row))
        else:
            left.append(_row_text([c for c in row if (c["x0"] + c["x1"]) / 2 < x]))
            right.append(_row_text([c for c in row if (c["x0"] + c["x1"]) / 2 >= x]))
    return [t for t in lines + left + right if t]


# --- DOCX ----------------------------------------------------------------------------------------------------------


def _read_docx(data: bytes) -> tuple[list[list[str]], _Hidden]:
    if data[:8] == _OLE_MAGIC:
        raise ResumeUnreadable("encrypted")
    hidden = _Hidden()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = archive.infolist()
            if len(parts) > MAX_DOCX_PARTS or sum(p.file_size for p in parts) > MAX_DOCX_UNZIPPED_BYTES:
                raise ResumeUnreadable("corrupt")
        document = Document(io.BytesIO(data))
        texts: list[str] = []
        for p in document.element.body.iter(qn("w:p")):  # every paragraph, table cells and text boxes included
            above = {a.tag for a in p.iterancestors()}
            if _MC_FALLBACK not in above:  # Word stores each text box twice
                text = _docx_paragraph(p, document, bool(above & {qn("w:tc"), qn("w:txbxContent")}), hidden)
                texts += [line for line in map(_clean, text.split("\n")) if line]
    except ResumeUnreadable:
        raise
    except Exception:  # untrusted bytes in a third-party parser: whatever it raises, the file is unreadable
        raise ResumeUnreadable("corrupt") from None
    return [texts], hidden


def _inherited(run: Run, paragraph: Paragraph, get: Any) -> Any:
    """A font property as Word resolves it: the run, then its character style chain, then the paragraph style chain.
    python-docx gives None for "not set here, inherit", so the first answer that is not None wins."""
    fonts = [run.font]
    for style in (run.style, paragraph.style):
        while style is not None:
            fonts.append(style.font)
            style = style.base_style
    for font in fonts:
        value = get(font)
        if value is not None:
            return value
    return None


def _docx_background(r: Any, p: Any, inside_box: bool) -> tuple[float, float, float] | None:
    """The fill behind a run: its own, its paragraph's or its table cell's shading; white for the plain page; None when
    the run sits in a table cell or text box with no shading we can read (the fill may be a style's or a shape's)."""
    fills = r.xpath("./w:rPr/w:shd/@w:fill") + p.xpath("./w:pPr/w:shd/@w:fill")
    fills += p.xpath("ancestor::w:tc[1]/w:tcPr/w:shd/@w:fill")
    for fill in fills:
        if re.fullmatch(r"[0-9A-Fa-f]{6}", fill):
            red, green, blue = (int(fill[i : i + 2], 16) / 255 for i in (0, 2, 4))
            return (red, green, blue)
    return None if inside_box else _WHITE


def _docx_run_reasons(run: Run, r: Any, p: Any, paragraph: Paragraph, inside_box: bool) -> set[str]:
    reasons: set[str] = set()
    if _inherited(run, paragraph, lambda f: f.hidden):
        reasons.add("docx_hidden_run")
    size = _inherited(run, paragraph, lambda f: f.size)
    if size is not None and size.pt < MIN_FONT_PT:
        reasons.add("tiny_font")
    colour = _inherited(run, paragraph, lambda f: f.color.rgb)
    if colour is not None:
        fill, behind = _rgb(tuple(c / 255 for c in colour)), _docx_background(r, p, inside_box)
        if fill is not None and behind is not None and _contrast(fill, behind) < MIN_CONTRAST:
            reasons.add("text_colour_matches_background")
    return reasons


def _docx_paragraph(p: Any, document: Any, inside_box: bool, hidden: _Hidden) -> str:
    """The visible text of one paragraph; its hidden runs go to `hidden`."""
    paragraph = Paragraph(p, document)
    visible: list[str] = []
    run_text: list[str] = []
    run_reasons: set[str] = set()
    for r in p.xpath("./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:smartTag/w:r | ./w:sdt/w:sdtContent/w:r"):
        run = Run(r, document)
        reasons = _docx_run_reasons(run, r, p, paragraph, inside_box)
        if reasons:
            run_text.append(run.text)
            run_reasons |= reasons
            continue
        if run_text:
            hidden.add("".join(run_text), run_reasons)
            run_text, run_reasons = [], set()
        visible.append(run.text)
    if run_text:
        hidden.add("".join(run_text), run_reasons)
    return "".join(visible)
