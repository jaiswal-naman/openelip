"""A resume document as blocks, as a PDF and as a DOCX (TLR-03). Rendered on demand from approved edits; nothing is
stored.

The PDF uses the built-in Helvetica, which draws Latin-1 only. A line with a character above U+00FF is refused with
UnsupportedCharacters instead of being drawn without it (decision 27): an export that quietly lost a letter of the
candidate's own resume would be worse than an error. The DOCX carries every character. No model call, no database.
"""

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape

import docx
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer  # type: ignore[import-untyped]

from .resume import ResumeDoc

TITLE, SECTION, ENTRY = 0, 1, 2  # the levels of a block heading
LATIN_1_LAST = 0xFF
BULLET = "-"  # a plain mark: the built-in font's bullet glyph is read back as (cid:127) by text extractors and ATS
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # a DOCX refuses these


class UnsupportedCharacters(ValueError):
    """The PDF cannot draw these characters. They are named in `characters`; the message holds no resume text."""

    def __init__(self, characters: Sequence[str]) -> None:
        super().__init__(f"pdf_unsupported_characters: {len(characters)} distinct characters outside Latin-1")
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

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13)
_STYLES = {
    TITLE: ParagraphStyle("title", parent=_BODY, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceAfter=2),
    SECTION: ParagraphStyle(
        "section", parent=_BODY, fontName="Helvetica-Bold", fontSize=12, leading=15, spaceBefore=12, spaceAfter=3
    ),
    ENTRY: ParagraphStyle("entry", parent=_BODY, fontName="Helvetica-Bold", spaceBefore=6),
}
_BULLET_STYLE = ParagraphStyle("bullet", parent=_BODY, leftIndent=14, bulletIndent=4, spaceAfter=1)


def _markup(text: str) -> str:
    """Text as reportlab markup: a < or & in a resume line is a character, not a tag."""
    return escape(text).replace("\n", "<br/>")


def _unsupported(blocks: Sequence[Block]) -> list[str]:
    return sorted({char for block in blocks for text in _texts(block) for char in text if ord(char) > LATIN_1_LAST})


def render_pdf(blocks: Sequence[Block]) -> bytes:
    """A4, plain styles, one column. The same blocks give the same bytes (invariant=1 fixes the dates and the id).
    Raises UnsupportedCharacters before drawing anything when a line holds a character above U+00FF."""
    # ponytail: Latin-1 only, so a resume with an en dash or a curly quote gets the DOCX; bundle a Unicode font when a
    # user needs the PDF for such a resume (INDEX decision 27, manual check 7).
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
