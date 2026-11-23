"""Builds the hidden-text test slice (AI_SYSTEM 4.1, requirement REC-06).

    python evals/resume_extraction/hidden/build_cases.py OUT_DIR

writes about sixty invented resume files into OUT_DIR and a manifest.json beside them. Every file is a short invented
resume (fictional names and employers, built from random.Random(3), so a rebuild gives the same text). Fifty-two hide
text in exactly one way, one of them a technique the reader cannot see yet; the rest are legitimate look-alikes that
must not be flagged. The files are never committed: the test builds them into a temporary directory each run.

The manifest lists, per file: the technique, the layout, the reason the reader should report, the hidden strings that
must be absent from the extracted text, and the visible lines that must survive (in the form the reader reports them).
"""

import json
import random
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SEED = 3

KNOWN_GAP = "pdf_invisible_render_mode"  # the technique the reader cannot see (pdfplumber does not expose Tr 3)

FIRST = ["Avery", "Rohan", "Mira", "Tomas", "Linnea", "Devika", "Kofi", "Priya", "Sven", "Ines"]
LAST = [
    "Fairweather",
    "Lindqvist",
    "Banerjee-Cole",
    "Ashdown",
    "Vasquez-Holt",
    "Marlowe",
    "Quillfeather",
    "Okafor-Reyes",
]
EMPLOYERS = ["Example Corp", "Brightline Health", "Kestrel Freight", "Tidewater Software", "Pinecrest Analytics"]
TITLES = ["Backend Engineer", "Data Analyst", "Platform Engineer", "QA Lead", "Mobile Developer", "Support Engineer"]
THINGS = ["a billing service", "an ingest pipeline", "a search index", "a mobile checkout", "a metrics dashboard"]
TECH = ["Go", "Python", "Kotlin", "TypeScript", "Rust", "Java"]
UNITS = ["users", "orders", "devices", "accounts"]
METRICS = ["p95 latency", "cloud spend", "build time", "alert noise"]
SKILLS = ["Go", "Python", "SQL", "Docker", "Kotlin", "Rust", "Linux", "Git"]
# Hidden strings. Long words only, so one never appears by accident inside the visible text.
KEYWORDS = ["kubernetes", "terraform", "snowflake", "airflow", "pytorch", "tensorflow", "kafka", "graphql", "ansible"]

# Hidden text laid under a visible line, glyph for glyph: same word lengths, no letter equal to the one above it.
# (visible line, hidden text) in a monospaced font, so each hidden letter sits exactly under a different visible one.
OVERLAP_PAIRS = [
    ("Senior Engineer at Example Corp", "golang redshift go kubectl rust"),
    ("Backend Developer at Initech Labs", "pytorch snowflake go airflow helm"),
    ("Data Analyst at Globex Systems", "rust mongodb go python grafana"),
    ("Platform Lead at Acme Cloud", "pipeline bash go ruby redis"),
]

Draw = Callable[[canvas.Canvas], None]


@dataclass
class Case:
    file: str
    kind: str  # "pdf" or "docx"
    mime: str
    technique: str
    layout: str
    reason: str | None  # the reason the reader should report; None for a legitimate look-alike
    hidden: list[str]  # strings that must be absent from the extracted text; empty for a look-alike
    visible: list[str]  # lines that must survive
    known_gap: bool = False


@dataclass
class Role:
    title: str
    employer: str
    dates: str
    bullets: list[str]


@dataclass
class Content:
    name: str
    headline: str
    side: list[str]
    roles: list[Role] = field(default_factory=list)

    @property
    def body(self) -> list[str]:
        return [line for r in self.roles for line in (f"{r.title}, {r.employer}", *r.bullets)]


def make_content(rng: random.Random) -> Content:
    def bullet() -> str:
        kind = rng.randrange(3)
        if kind == 0:
            line = f"Built {rng.choice(THINGS)} in {rng.choice(TECH)} for {rng.randrange(20, 900)}k {rng.choice(UNITS)}"
        elif kind == 1:
            scope = rng.choice(["six services", "two regions"])
            line = f"Cut {rng.choice(METRICS)} by {rng.randrange(15, 70)}% across {scope}"
        else:
            line = f"Led {rng.randrange(3, 12)} engineers through a {rng.choice(['migration', 'rewrite', 'launch'])}"
        assert len(line) <= 58, line  # fits the narrow column of the two-column layouts
        return line

    years = rng.randrange(2012, 2018)
    roles = [
        Role(rng.choice(TITLES), rng.choice(EMPLOYERS), f"{years + 3 * i} - {years + 3 * i + 3}", [bullet(), bullet()])
        for i in range(3)
    ]
    side = [
        f"Skills: {', '.join(rng.sample(SKILLS, 3))}",
        f"Tools: {', '.join(rng.sample(SKILLS, 3))}",
        "Languages: English, Hindi",
        f"Education: B.Tech, class of {years - 1}",
        "Interests: chess, trail running",
    ]
    return Content(f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(TITLES), side, roles)


# --- PDF layouts: each draws the visible resume and returns the lines the reader should report --------------------


def _lines(c: canvas.Canvas, lines: list[str], x: float, top: float, step: float, size: float = 10) -> None:
    c.setFont("Helvetica", size)
    for i, text in enumerate(lines):
        c.drawString(x, top - step * i, text)


def single(c: canvas.Canvas, content: Content) -> list[str]:
    visible = [content.name, *content.side, *content.body]
    _lines(c, visible, 50, 740, 15)
    return visible


def compact(c: canvas.Canvas, content: Content) -> list[str]:
    visible = [content.name, *content.side, *content.body]
    _lines(c, visible, 60, 740, 12, size=9)
    return visible


def two_column(c: canvas.Canvas, content: Content) -> list[str]:
    side, body = [content.name, *content.side], content.body
    for i in range(max(len(side), len(body))):  # painted row by row, so only the geometry can order it
        if i < len(side):
            _lines(c, [side[i]], 40, 740 - 15 * i, 15)
        if i < len(body):
            _lines(c, [body[i]], 290, 740 - 15 * i, 15)
    return side + body


def banner_columns(c: canvas.Canvas, content: Content) -> list[str]:
    banner = f"{content.name} | {content.headline}"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, 760, banner)
    c.setFont("Helvetica", 10)
    for i in range(max(len(content.side), len(content.body))):
        if i < len(content.side):
            c.drawString(40, 730 - 15 * i, content.side[i])
        if i < len(content.body):
            c.drawString(290, 730 - 15 * i, content.body[i])
    return [banner, *content.side, *content.body]


def dated(c: canvas.Canvas, content: Content) -> list[str]:
    visible = [content.name, *content.side]
    _lines(c, visible, 50, 740, 15)
    y = 740 - 15 * len(visible) - 10
    for role in content.roles:
        c.drawString(50, y, f"{role.title}, {role.employer}")
        c.drawString(480, y, role.dates)
        _lines(c, role.bullets, 50, y - 14, 14)
        visible += [f"{role.title}, {role.employer} {role.dates}", *role.bullets]
        y -= 14 * (len(role.bullets) + 1) + 10
    return visible


LAYOUTS: dict[str, Callable[[canvas.Canvas, Content], list[str]]] = {
    "single": single,
    "two_column": two_column,
    "banner_columns": banner_columns,
    "dated": dated,
    "compact": compact,
}

# --- PDF techniques: each hides `hidden` in the free band below the resume (y about 300 to 440) -------------------
Hide = Callable[[canvas.Canvas, str], None]


def _tiny(size: float) -> Hide:
    def apply(c: canvas.Canvas, hidden: str) -> None:
        c.setFont("Helvetica", size)
        c.drawString(50, 400, hidden)

    return apply


def _at(x: float, y: float) -> Hide:
    return lambda c, hidden: c.drawString(x, y, hidden)


def _coloured(set_colour: Callable[[canvas.Canvas], None], box: Callable[[canvas.Canvas], None] | None = None) -> Hide:
    def apply(c: canvas.Canvas, hidden: str) -> None:
        if box:
            box(c)
        set_colour(c)
        c.drawString(50, 400, hidden)

    return apply


def _fill_box(red: float, green: float, blue: float) -> Callable[[canvas.Canvas], None]:
    def draw(c: canvas.Canvas) -> None:
        c.setFillColorRGB(red, green, blue)
        c.rect(40, 385, 320, 30, fill=1, stroke=0)

    return draw


# (technique, the reason the reader should give, how to hide). Sixteen, each built on two layouts.
PDF_TECHNIQUES: list[tuple[str, str, Hide]] = [
    ("tiny_1pt", "tiny_font", _tiny(1)),
    ("tiny_2pt", "tiny_font", _tiny(2)),
    ("tiny_3pt", "tiny_font", _tiny(3)),
    ("tiny_just_under_4pt", "tiny_font", _tiny(3.9)),
    ("off_page_left", "off_page", _at(-320, 400)),
    ("off_page_right", "off_page", _at(640, 400)),
    ("off_page_below", "off_page", _at(50, -30)),
    ("off_page_above", "off_page", _at(50, 840)),
    ("white_on_white", "text_colour_matches_background", _coloured(lambda c: c.setFillColorRGB(1, 1, 1))),
    ("near_white_on_white", "text_colour_matches_background", _coloured(lambda c: c.setFillGray(0.99))),
    (
        "white_on_white_box",
        "text_colour_matches_background",
        _coloured(lambda c: c.setFillColorRGB(1, 1, 1), _fill_box(1, 1, 1)),
    ),
    ("light_grey_90", "text_colour_matches_background", _coloured(lambda c: c.setFillGray(0.9))),
    ("light_grey_95", "text_colour_matches_background", _coloured(lambda c: c.setFillGray(0.95))),
    (
        "black_on_dark_box",
        "text_colour_matches_background",
        _coloured(lambda c: c.setFillColorRGB(0, 0, 0), _fill_box(0.1, 0.1, 0.1)),
    ),
    (
        "navy_on_navy_box",
        "text_colour_matches_background",
        _coloured(lambda c: c.setFillColorRGB(0.07, 0.12, 0.33), _fill_box(0.05, 0.1, 0.3)),
    ),
    ("cmyk_white", "text_colour_matches_background", _coloured(lambda c: c.setFillColorCMYK(0, 0, 0, 0))),
]


def _aligned(visible: str, hidden: str) -> bool:
    words = zip(visible.split(" "), hidden.split(" "), strict=True)
    return all(len(v) == len(h) and all(a != b for a, b in zip(v, h, strict=True)) for v, h in words)


def _write_pdf(path: Path, draw: Draw) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)
    draw(c)
    c.showPage()
    c.save()


def _pdf_cases(out: Path, rng: random.Random, counter: Callable[[], int]) -> list[Case]:
    cases: list[Case] = []
    names = list(LAYOUTS)

    def add(
        technique: str, layout: str, reason: str | None, hidden: list[str], draw: Callable[[canvas.Canvas], list[str]]
    ):
        file = f"{counter():03d}-pdf-{technique}-{layout}.pdf"
        visible: list[str] = []

        def run(c: canvas.Canvas) -> None:
            visible.extend(draw(c))

        _write_pdf(out / file, run)
        assert not any(h in line for h in hidden for line in visible), file  # a hidden string is never also visible
        cases.append(Case(file, "pdf", PDF_MIME, technique, layout, reason, hidden, visible, technique == KNOWN_GAP))

    def with_hidden(layout: str, content: Content, hidden: str, hide: Hide) -> Callable[[canvas.Canvas], list[str]]:
        def draw(c: canvas.Canvas) -> list[str]:
            visible = LAYOUTS[layout](c, content)
            hide(c, hidden)
            return visible

        return draw

    for i, (technique, reason, hide) in enumerate(PDF_TECHNIQUES):
        for layout in (names[i % len(names)], names[(i + 2) % len(names)]):
            hidden = " ".join(rng.sample(KEYWORDS, 3))
            add(technique, layout, reason, [hidden], with_hidden(layout, make_content(rng), hidden, hide))

    for i, (shown, hidden) in enumerate(OVERLAP_PAIRS):
        assert _aligned(shown, hidden), shown
        layout, content = names[i % len(names)], make_content(rng)

        def covered(c: canvas.Canvas, layout=layout, content=content, shown=shown, hidden=hidden) -> list[str]:
            visible = LAYOUTS[layout](c, content)
            c.setFont("Courier", 11)
            c.drawString(50, 360, hidden)  # drawn first, so the visible line is painted over it
            c.drawString(50, 360, shown)
            return [*visible, shown]

        add("covered_by_visible_line", layout, "overlapped", [hidden], covered)

    gap_content = make_content(rng)
    gap_hidden = " ".join(rng.sample(KEYWORDS, 3))

    def invisible_render_mode(c: canvas.Canvas) -> list[str]:
        visible = single(c, gap_content)
        text = c.beginText(50, 400)
        text.setFont("Helvetica", 10)
        text.setTextRenderMode(3)  # neither filled nor stroked: invisible, yet extracted as ordinary text
        text.textLine(gap_hidden)
        c.drawText(text)
        return visible

    # The reason is the one a future detector would need to add to the reader's vocabulary.
    add(KNOWN_GAP, "single", "invisible_render_mode", [gap_hidden], invisible_render_mode)
    return cases + _pdf_look_alikes(out, rng, counter)


def _pdf_look_alikes(out: Path, rng: random.Random, counter: Callable[[], int]) -> list[Case]:
    """Files that look like hiding but are legitimate. Each must keep every visible line and report nothing."""
    cases = []

    def add(technique: str, layout: str, draw: Callable[[canvas.Canvas], list[str]]) -> None:
        file = f"{counter():03d}-pdf-{technique}-{layout}.pdf"
        visible: list[str] = []
        _write_pdf(out / file, lambda c: visible.extend(draw(c)))
        cases.append(Case(file, "pdf", PDF_MIME, technique, layout, None, [], visible))

    def sidebar(layout: str) -> Callable[[canvas.Canvas], list[str]]:
        extra = ["Python, Go and SQL", "Docker and Linux", "Kafka and Postgres"]

        def draw(c: canvas.Canvas) -> list[str]:
            visible = LAYOUTS[layout](c, make_content(rng))
            c.setFillColorRGB(0.1, 0.1, 0.1)
            c.rect(40, 330, 200, 70, fill=1, stroke=0)
            c.setFillColorRGB(1, 1, 1)
            _lines(c, extra, 55, 385, 18, size=11)
            return [*visible, *extra]

        return draw

    for layout in ("single", "two_column", "compact"):
        add("white_on_dark_sidebar", layout, sidebar(layout))

    def with_extra(
        layout: str, extra: str, style: Callable[[canvas.Canvas], None]
    ) -> Callable[[canvas.Canvas], list[str]]:
        def draw(c: canvas.Canvas) -> list[str]:
            visible = LAYOUTS[layout](c, make_content(rng))
            style(c)
            c.drawString(50, 400, extra)
            return [*visible, extra]

        return draw

    add(
        "four_point_footnote",
        "single",
        with_extra("single", "Available for relocation within two weeks", lambda c: c.setFont("Helvetica", 4)),
    )
    add(
        "faint_grey_80",
        "single",
        with_extra("single", "References on request, in confidence", lambda c: c.setFillGray(0.8)),
    )

    def faux_bold(c: canvas.Canvas) -> list[str]:
        content = make_content(rng)
        visible = single(c, content)
        c.translate(0.3, 0)  # the same lines again, 0.3 pt to the right: how some producers fake bold
        single(c, content)
        return visible

    add("faux_bold_double_strike", "single", faux_bold)
    return cases


# --- DOCX ----------------------------------------------------------------------------------------------------------


def _shade(parent, fill: str) -> None:
    shading = OxmlElement("w:shd")
    for name, value in (("val", "clear"), ("color", "auto"), ("fill", fill)):
        shading.set(qn(f"w:{name}"), value)
    parent.append(shading)


def _paragraphs(container, lines: list[str], style: str | None = None) -> None:
    for line in lines:
        container.add_paragraph(line, style=style)


def _docx_layout(document, layout: str, content: Content) -> list[str]:
    """Adds the visible resume. `table` puts the sidebar and the body in two table cells."""
    if layout == "table":
        cells = document.add_table(rows=1, cols=2).rows[0].cells
        _paragraphs(cells[0], content.side)
        _paragraphs(cells[1], content.body)
        return [*content.side, *content.body]
    document.add_heading(content.name, 1)
    _paragraphs(document, content.side)
    _paragraphs(document, content.body, "List Bullet" if layout == "bullets" else None)
    return [content.name, *content.side, *content.body]


def _conceal(document, kind: str) -> str:
    """Defines a hidden style and returns its name. kind: character, paragraph or chain."""
    styles = document.styles
    if kind == "paragraph":
        styles.add_style("Concealed", WD_STYLE_TYPE.PARAGRAPH).font.hidden = True
        return "Concealed"
    parent = styles.add_style("Concealed", WD_STYLE_TYPE.CHARACTER)
    parent.font.hidden = True
    if kind == "character":
        return "Concealed"
    child = styles.add_style("ConcealedChild", WD_STYLE_TYPE.CHARACTER)
    child.base_style = parent
    return "ConcealedChild"


# (technique, layout, reason, how). `how(document, hidden)` adds the hidden text and returns the extra visible lines.
def _hidden_paragraph(document, hidden: str) -> list[str]:
    document.add_paragraph().add_run(hidden).font.hidden = True
    return []


def _hidden_mid_paragraph(document, hidden: str) -> list[str]:
    paragraph = document.add_paragraph("References are available on request ")
    paragraph.add_run(hidden).font.hidden = True
    paragraph.add_run(" and a portfolio too")
    return ["References are available on request and a portfolio too"]


def _two_hidden_runs(document, hidden: str) -> list[str]:
    first, second = hidden.split(" ", 1)
    paragraph = document.add_paragraph("Start ")
    paragraph.add_run(first).font.hidden = True
    paragraph.add_run(" middle ")
    paragraph.add_run(second).font.hidden = True
    paragraph.add_run(" end")
    return ["Start middle end"]


def _in_cell(document, hidden: str) -> list[str]:
    cell = document.tables[0].rows[0].cells[1]
    cell.add_paragraph().add_run(hidden).font.hidden = True
    return []


def _styled(kind: str):
    def how(document, hidden: str) -> list[str]:
        style = _conceal(document, kind)
        if kind == "paragraph":
            document.add_paragraph(hidden, style=style)
        else:
            document.add_paragraph().add_run(hidden).style = style
        return []

    return how


def _white(document, hidden: str) -> list[str]:
    document.add_paragraph().add_run(hidden).font.color.rgb = RGBColor(255, 255, 255)
    return []


def _small(points: float):
    def how(document, hidden: str) -> list[str]:
        document.add_paragraph().add_run(hidden).font.size = Pt(points)
        return []

    return how


DOCX_TECHNIQUES = [
    ("hidden_run", "plain", "docx_hidden_run", _hidden_paragraph),
    ("hidden_run_mid_paragraph", "plain", "docx_hidden_run", _hidden_mid_paragraph),
    ("hidden_run_in_table_cell", "table", "docx_hidden_run", _in_cell),
    ("hidden_run", "bullets", "docx_hidden_run", _hidden_paragraph),
    ("two_hidden_runs", "plain", "docx_hidden_run", _two_hidden_runs),
    ("hidden_character_style", "plain", "docx_hidden_run", _styled("character")),
    ("hidden_character_style", "table", "docx_hidden_run", _styled("character")),
    ("hidden_paragraph_style", "plain", "docx_hidden_run", _styled("paragraph")),
    ("hidden_paragraph_style", "bullets", "docx_hidden_run", _styled("paragraph")),
    ("hidden_style_chain", "plain", "docx_hidden_run", _styled("chain")),
    ("white_text", "plain", "text_colour_matches_background", _white),
    ("white_text", "bullets", "text_colour_matches_background", _white),
    ("tiny_text_1pt", "plain", "tiny_font", _small(1)),
    ("tiny_text_2pt", "bullets", "tiny_font", _small(2)),
    ("tiny_text_3pt", "table", "tiny_font", _small(3)),
]


def _docx_cases(out: Path, rng: random.Random, counter: Callable[[], int]) -> list[Case]:
    cases = []

    def add(technique: str, layout: str, reason: str | None, hidden: list[str], build: Callable) -> None:
        file = f"{counter():03d}-docx-{technique}-{layout}.docx"
        document = Document()
        visible = build(document)
        document.save(str(out / file))
        assert not any(h in line for h in hidden for line in visible), file
        cases.append(Case(file, "docx", DOCX_MIME, technique, layout, reason, hidden, visible))

    for technique, layout, reason, how in DOCX_TECHNIQUES:
        hidden = " ".join(rng.sample(KEYWORDS, 3))
        content = make_content(rng)

        def build(document, layout=layout, content=content, hidden=hidden, how=how) -> list[str]:
            visible = _docx_layout(document, layout, content)
            return [*visible, *how(document, hidden)]

        add(technique, layout, reason, [hidden], build)

    def shaded_paragraph(document) -> list[str]:
        visible = _docx_layout(document, "plain", make_content(rng))
        paragraph = document.add_paragraph()
        _shade(paragraph._p.get_or_add_pPr(), "1F2937")
        paragraph.add_run("Python, Go and SQL").font.color.rgb = RGBColor(255, 255, 255)
        return [*visible, "Python, Go and SQL"]

    def shaded_cell(document) -> list[str]:
        visible = _docx_layout(document, "table", make_content(rng))
        cell = document.tables[0].rows[0].cells[0]
        _shade(cell._tc.get_or_add_tcPr(), "1F2937")
        cell.add_paragraph().add_run("Docker and Linux").font.color.rgb = RGBColor(255, 255, 255)
        return [*visible, "Docker and Linux"]

    def explicit_visible(document) -> list[str]:
        visible = _docx_layout(document, "plain", make_content(rng))
        _conceal(document, "character")
        run = document.add_paragraph().add_run("Kafka and Postgres")
        run.style = "Concealed"
        run.font.hidden = False  # the run says it is shown, so the style's "hidden" does not apply
        return [*visible, "Kafka and Postgres"]

    add("white_on_shaded_paragraph", "plain", None, [], shaded_paragraph)
    add("white_on_shaded_cell", "table", None, [], shaded_cell)
    add("explicitly_visible_run", "plain", None, [], explicit_visible)
    return cases


def build_cases(out_dir: Path) -> list[Case]:
    """Writes the slice into out_dir (created if needed) with a manifest.json, and returns the cases."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    numbers = iter(range(1, 1000))
    counter = lambda: next(numbers)  # noqa: E731
    cases = _pdf_cases(out, rng, counter) + _docx_cases(out, rng, counter)
    (out / "manifest.json").write_text(json.dumps([asdict(c) for c in cases], indent=2), encoding="utf-8")
    return cases


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: build_cases.py OUT_DIR")
    print(f"{len(build_cases(Path(sys.argv[1])))} files written to {sys.argv[1]}")
