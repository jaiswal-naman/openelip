"""A resume document as blocks, as a PDF and as a DOCX (TLR-03). Rendered on demand from approved edits; nothing is
stored.

The PDF embeds Bitstream Vera (regular and bold), the TrueType font reportlab itself ships, so no font file lives in
this repository. It draws Latin-1, the punctuation a real resume carries (en and em dashes, curly quotes, bullet,
ellipsis, euro) and some Latin Extended; not the rupee sign, most Latin Extended-A letters, Greek, Cyrillic, CJK or
emoji. A line with a character the font cannot draw is refused with UnsupportedCharacters instead of being drawn
without it (decision 27): an export that quietly lost a letter of the candidate's own resume would be worse than an
error. The DOCX carries every character. No model call, no database.
"""

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import docx
import reportlab  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # type: ignore[import-untyped]

from .resume import ResumeDoc

TITLE, SECTION, ENTRY = 0, 1, 2  # the levels of a block heading
BULLET = "-"  # a plain mark: every text extractor and ATS reads it back as itself
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # a DOCX refuses these

# To draw more scripts (the rupee sign, Cyrillic, Greek), point these two at a wider TrueType family: the check below
# reads the font's own character map, so nothing else changes.
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
_REGULAR, _BOLD = "Vera", "Vera-Bold"
pdfmetrics.registerFont(TTFont(_REGULAR, str(_FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont(_BOLD, str(_FONT_DIR / "VeraBd.ttf")))
# A character both weights can draw: headings are bold, everything else regular.
_DRAWABLE = frozenset(pdfmetrics.getFont(_REGULAR).face.charToGlyph) & frozenset(
    pdfmetrics.getFont(_BOLD).face.charToGlyph
)


class UnsupportedCharacters(ValueError):
    """The PDF cannot draw these characters. They are named in `characters`; the message holds no resume text."""

    def __init__(self, characters: Sequence[str]) -> None:
        super().__init__(f"pdf_unsupported_characters: {len(characters)} distinct characters the PDF font cannot draw")
        self.characters = tuple(characters)


@dataclass(frozen=True)
class Block:
    """One part of a document: a heading, plain lines, then bullets. level is TITLE (the name), SECTION or ENTRY."""

    heading: str
    lines: tuple[str, ...] = ()
    bullets: tuple[str, ...] = ()
    level: int = SECTION


def resume_blocks(doc: ResumeDoc) -> list[Block]:
    """The document as blocks, in reading order. A section with nothing in it is left out."""
    blocks: list[Block] = []
    if doc.name or doc.email:
        blocks.append(Block(doc.name or doc.email, (doc.email,) if doc.name and doc.email else (), (), TITLE))
    if doc.roles:
        blocks.append(Block("Experience"))
        for role in doc.roles:
            blocks.append(
                Block(role.header, (role.dates,) if role.dates else (), tuple(b.text for b in role.bullets), ENTRY)
            )
    if doc.skills:
        blocks.append(Block("Skills", (", ".join(b.text for b in doc.skills),)))
    if doc.education:
        blocks.append(Block("Education", tuple(b.text for b in doc.education)))
    if doc.certifications:
        blocks.append(Block("Certifications", tuple(b.text for b in doc.certifications)))
    if doc.projects:
        blocks.append(Block("Projects", (), tuple(b.text for b in doc.projects)))
    return blocks


def _texts(block: Block) -> list[str]:
    return [block.heading, *block.lines, *block.bullets]


# ---- PDF ----------------------------------------

_BODY = ParagraphStyle("body", fontName=_REGULAR, fontSize=10, leading=13)
_STYLES = {
    TITLE: ParagraphStyle("title", parent=_BODY, fontName=_BOLD, fontSize=18, leading=22, spaceAfter=2),
    SECTION: ParagraphStyle(
        "section", parent=_BODY, fontName=_BOLD, fontSize=12, leading=15, spaceBefore=12, spaceAfter=3
    ),
    ENTRY: ParagraphStyle("entry", parent=_BODY, fontName=_BOLD, spaceBefore=6),
}
_BULLET_STYLE = ParagraphStyle(
    "bullet", parent=_BODY, bulletFontName=_REGULAR, leftIndent=14, bulletIndent=4, spaceAfter=1
)


def _markup(text: str) -> str:
    """Text as reportlab markup: a < or & in a resume line is a character, not a tag."""
    return escape(text).replace("\n", "<br/>")


def _unsupported(blocks: Sequence[Block]) -> list[str]:
    """The characters the font cannot draw. Whitespace is not drawn at all: the paragraph folds it into the gap between
    words, so a tab or a thin space is not a lost character."""
    return sorted(
        {
            char
            for block in blocks
            for text in _texts(block)
            for char in text
            if ord(char) not in _DRAWABLE and not char.isspace()
        }
    )


def render_pdf(blocks: Sequence[Block]) -> bytes:
    """A4, plain styles, one column. The same blocks give the same bytes (invariant=1 fixes the dates and the id).
    Raises UnsupportedCharacters before drawing anything when a line holds a character the font cannot draw."""
    if refused := _unsupported(blocks):
        raise UnsupportedCharacters(refused)
    story: list[Paragraph | Spacer] = []
    for block in blocks:
        story.append(Paragraph(_markup(block.heading), _STYLES.get(block.level, _STYLES[SECTION])))
        story.extend(Paragraph(_markup(line), _BODY) for line in block.lines)
        story.extend(Paragraph(_markup(item), _BULLET_STYLE, bulletText=BULLET) for item in block.bullets)
    buffer = io.BytesIO()
    margin = 20 * mm
    SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        invariant=1,
    ).build(story or [Spacer(1, 1)])
    return buffer.getvalue()


# ---- DOCX ----------------------------------------


def _clean(text: str) -> str:
    return _XML_BAD.sub("", text)


def render_docx(doc: ResumeDoc) -> bytes:
    """The same blocks as the PDF, in Word's own heading and bullet styles. Every character is kept."""
    document = docx.Document()
    for block in resume_blocks(doc):
        document.add_heading(_clean(block.heading), level=block.level)
        for line in block.lines:
            document.add_paragraph(_clean(line))
        for item in block.bullets:
            document.add_paragraph(_clean(item), style="List Bullet")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
