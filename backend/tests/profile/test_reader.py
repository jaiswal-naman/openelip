"""The resume reader: numbered lines with offsets, reading order, hidden text removed and reported, unreadable files.

Every file is built here, small, from invented text with reportlab (PDF) and python-docx (DOCX).
"""

import io
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from PIL import Image
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.profile.resume.reader import (
    DOCX_MIME,
    MIN_CHARS_PER_PAGE,
    MIN_FONT_PT,
    PDF_MIME,
    REASONS,
    ResumeUnreadable,
    read_resume,
)

# About 300 visible characters: enough that a page is not mistaken for a scan.
BODY = [
    "Senior Backend Engineer, Example Corp, built payment services in Go and Python for ten million users",
    "Led a team of six engineers and cut p95 latency from 900 ms to 180 ms across the checkout services",
    "Designed an event-driven billing pipeline on PostgreSQL and queues, reducing failed invoices by half",
]
LEFT = [
    "Skills: Python, Go, SQL, Docker",
    "Cloud: AWS, GCP, Terraform, Kubernetes",
    "Tools: Git, Linux, Grafana, Prometheus",
    "Languages: English, Hindi, German, French",
    "Certifications: AWS Solutions Architect",
    "Interests: chess, trail running, open source",
]
RIGHT = [
    "Senior Backend Engineer, Example Corp, Go",
    "Led six engineers, p95 latency 900 to 180 ms",
    "Designed a billing pipeline on PostgreSQL",
    "Mentored four junior engineers, on-call lead",
    "B.Tech Computer Science, Example University",
    "Open-source queue library, two thousand stars",
]
WHITE, DARK = (1, 1, 1), (0.1, 0.1, 0.1)

Draw = Callable[[canvas.Canvas], None]


def pdf(*pages: Draw, **canvas_options: Any) -> bytes:
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=LETTER, **canvas_options)
    for draw in pages:
        page.setFont("Helvetica", 11)
        draw(page)
        page.showPage()
    page.save()
    return out.getvalue()


def write(lines: list[str], x: float = 50, top: float = 700, step: float = 16, size: float = 11) -> Draw:
    def draw(c: canvas.Canvas) -> None:
        c.setFont("Helvetica", size)
        for i, text in enumerate(lines):
            c.drawString(x, top - step * i, text)

    return draw


def docx(build: Callable[[Any], None], body: bool = True) -> bytes:
    document = Document()
    for text in BODY if body else []:
        document.add_paragraph(text)
    build(document)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def texts(result) -> list[str]:
    return [line.text for line in result.lines]


# --- lines and offsets ---------------------------------------------------------------------------------------------


def test_a_two_line_pdf_gives_lines_one_and_two_whose_offsets_index_the_text():
    long_lines = [  # over 200 characters in two lines, so the page is not taken for a scan
        "Senior Backend Engineer, Example Corp, built payment services in Go and Python for ten million users, "
        "led a team of six",
        "Designed an event-driven billing pipeline on PostgreSQL and queues, reducing failed invoices by half "
        "and cutting p95 latency",
    ]

    result = read_resume(pdf(write(long_lines, size=8)), PDF_MIME)

    assert [line.n for line in result.lines] == [1, 2]
    assert [line.page for line in result.lines] == [1, 1]
    assert texts(result) == long_lines
    assert result.text == "\n".join(long_lines)
    assert all(result.text[line.start : line.end] == line.text for line in result.lines)
    assert not result.scanned and not result.hidden.found


def test_lines_are_numbered_across_pages_and_carry_their_page():
    result = read_resume(pdf(write(BODY), write(BODY)), PDF_MIME)

    assert [line.n for line in result.lines] == [1, 2, 3, 4, 5, 6]
    assert [line.page for line in result.lines] == [1, 1, 1, 2, 2, 2]
    assert all(result.text[line.start : line.end] == line.text for line in result.lines)


def test_a_line_drawn_twice_to_fake_bold_is_read_once():
    def faux_bold(c):
        write(BODY, x=50)(c)
        write(BODY, x=50.3)(c)

    result = read_resume(pdf(faux_bold), PDF_MIME)

    assert texts(result) == BODY
    assert not result.hidden.found


# --- reading order -------------------------------------------------------------------------------------------------


def two_columns(c):
    for i, (left, right) in enumerate(zip(LEFT, RIGHT, strict=True)):  # painted row by row, not column by column
        c.drawString(40, 700 - 16 * i, left)
        c.drawString(290, 700 - 16 * i, right)


def test_a_two_column_pdf_reads_the_left_column_fully_before_the_right():
    result = read_resume(pdf(two_columns), PDF_MIME)

    assert texts(result) == LEFT + RIGHT


def test_a_full_width_banner_over_two_columns_comes_first_and_stays_whole():
    def with_banner(c):
        c.setFont("Helvetica-Bold", 18)
        c.drawString(110, 750, "Jane Example, Backend Engineer, Springfield")
        c.setFont("Helvetica", 11)
        two_columns(c)

    result = read_resume(pdf(with_banner), PDF_MIME)

    assert texts(result) == ["Jane Example, Backend Engineer, Springfield", *LEFT, *RIGHT]


def test_right_aligned_dates_stay_on_their_line_in_a_single_column():
    def dated(c):
        for i, text in enumerate(BODY):
            c.drawString(50, 700 - 32 * i, f"Role {i}, Example Corp")
            c.drawString(480, 700 - 32 * i, "2019 - 2021")
            c.drawString(50, 700 - 32 * i - 14, text)

    result = read_resume(pdf(dated), PDF_MIME)

    assert texts(result) == [
        line for i, text in enumerate(BODY) for line in (f"Role {i}, Example Corp 2019 - 2021", text)
    ]


# --- hidden text in a PDF ------------------------------------------------------------------------------------------

HIDDEN = "kubernetes golang rust"  # 20 characters without the spaces


def hide(technique: Callable[[canvas.Canvas], None]) -> bytes:
    return pdf(lambda c: (write(BODY)(c), technique(c)))


def tiny(c):
    c.setFont("Helvetica", MIN_FONT_PT - 0.1)
    c.drawString(50, 600, HIDDEN)


def off_left(c):
    c.drawString(-300, 600, HIDDEN)


def off_below(c):
    c.drawString(50, -40, HIDDEN)


def white_on_white(c):
    c.setFillColorRGB(*WHITE)
    c.drawString(50, 600, HIDDEN)


def grey_on_white(c):
    c.setFillGray(0.9)  # contrast 1.25
    c.drawString(50, 600, HIDDEN)


def dark_on_dark(c):
    c.setFillColorRGB(*DARK)
    c.rect(40, 590, 300, 30, fill=1, stroke=0)
    c.drawString(50, 600, HIDDEN)  # the fill is still the dark one


def cmyk_white(c):
    c.setFillColorCMYK(0, 0, 0, 0)
    c.drawString(50, 600, HIDDEN)


def covered(c):
    # Equal glyph widths and the same word lengths: each hidden letter sits exactly under a different visible letter.
    c.setFont("Courier", 11)
    c.drawString(50, 500, "golang redshift go kubectl rust")
    c.drawString(50, 500, "Senior Engineer at Example Corp")


TECHNIQUES = [
    pytest.param(tiny, "tiny_font", HIDDEN, id="tiny"),
    pytest.param(off_left, "off_page", HIDDEN, id="off-page-left"),
    pytest.param(off_below, "off_page", HIDDEN, id="off-page-below"),
    pytest.param(white_on_white, "text_colour_matches_background", HIDDEN, id="white-on-white"),
    pytest.param(grey_on_white, "text_colour_matches_background", HIDDEN, id="light-grey"),
    pytest.param(dark_on_dark, "text_colour_matches_background", HIDDEN, id="dark-on-dark"),
    pytest.param(cmyk_white, "text_colour_matches_background", HIDDEN, id="cmyk-white"),
    pytest.param(covered, "overlapped", "golang redshift go kubectl rust", id="covered"),
]


@pytest.mark.parametrize(("technique", "reason", "hidden_text"), TECHNIQUES)
def test_hidden_text_is_removed_and_reported_with_its_reason(technique, reason, hidden_text):
    result = read_resume(hide(technique), PDF_MIME)

    assert result.hidden.found
    assert result.hidden.reasons == [reason]
    assert result.hidden.chars_removed == len(hidden_text.replace(" ", ""))
    assert result.hidden.samples == [hidden_text]
    assert hidden_text not in result.text
    assert all(word not in result.text for word in hidden_text.split())
    assert set(BODY) <= set(texts(result))  # nothing visible was lost


def test_white_text_on_a_dark_filled_sidebar_is_legitimate_and_stays():
    def sidebar(c):
        c.setFillColorRGB(*DARK)
        c.rect(400, 480, 160, 100, fill=1, stroke=0)
        c.setFillColorRGB(*WHITE)
        c.drawString(410, 540, "Python, Go and SQL")

    result = read_resume(hide(sidebar), PDF_MIME)

    assert not result.hidden.found
    assert "Python, Go and SQL" in texts(result)


def test_text_over_an_image_is_not_judged_because_the_picture_behind_it_is_unknown():
    def over_a_photo(c):
        # A real dark image, not a shape: reportlab draws it with an image operator.
        photo = Image.new("RGB", (40, 40), (20, 20, 20))
        c.drawImage(ImageReader(photo), 400, 480, width=160, height=100)
        c.setFillColorRGB(*WHITE)
        c.drawString(410, 540, "Python, Go and SQL")

    result = read_resume(hide(over_a_photo), PDF_MIME)

    assert not result.hidden.found
    assert "Python, Go and SQL" in texts(result)


@pytest.mark.parametrize(
    "legitimate",
    [
        pytest.param(
            lambda c: (c.setFont("Helvetica", MIN_FONT_PT), c.drawString(50, 600, HIDDEN)), id="four-point-text"
        ),
        pytest.param(lambda c: (c.setFillGray(0.8), c.drawString(50, 600, HIDDEN)), id="faint-but-readable-grey"),
        pytest.param(lambda c: (c.setFillColorCMYK(0, 0, 0, 1), c.drawString(50, 600, HIDDEN)), id="cmyk-black"),
        pytest.param(lambda c: c.drawString(-2, 600, HIDDEN), id="hanging-two-points-over-the-edge"),
    ],
)
def test_the_thresholds_sit_where_the_specification_puts_them(legitimate):
    result = read_resume(hide(legitimate), PDF_MIME)

    assert not result.hidden.found
    assert HIDDEN in texts(result)


def test_samples_are_at_most_three_and_forty_characters_and_chars_removed_counts_every_one():
    strings = [HIDDEN, "a" * 60, "terraform docker", "ansible helm", "prometheus grafana"]

    def many(c):
        for i, text in enumerate(strings):
            c.setFillColorRGB(*WHITE)
            c.drawString(50, 600 - 14 * i, text)

    result = read_resume(hide(many), PDF_MIME)

    assert result.hidden.samples == [HIDDEN, "a" * 40, "terraform docker"]  # one per row, cut to forty characters
    assert result.hidden.chars_removed == sum(len(s.replace(" ", "")) for s in strings)


def test_a_hidden_space_under_a_visible_letter_does_not_split_the_word():
    def under_the_word(c):
        c.setFont("Courier", 11)  # one cell per character: the hidden space sits under the visible "E"
        c.drawString(50, 500, "golang redshif")
        c.drawString(50, 500, "SeniorEngineer")

    result = read_resume(hide(under_the_word), PDF_MIME)

    assert "SeniorEngineer" in texts(result)
    assert result.hidden.reasons == ["overlapped"]


def test_reasons_use_the_fixed_vocabulary_in_its_order():
    def both(c):
        tiny(c)
        white_on_white(c)

    result = read_resume(hide(both), PDF_MIME)

    assert result.hidden.reasons == ["tiny_font", "text_colour_matches_background"]
    assert set(result.hidden.reasons) <= set(REASONS)


# --- hidden text in a DOCX -----------------------------------------------------------------------------------------


def shade(parent: Any, fill: str) -> None:
    shading = OxmlElement("w:shd")
    for name, value in (("val", "clear"), ("color", "auto"), ("fill", fill)):
        shading.set(qn(f"w:{name}"), value)
    parent.append(shading)


def hidden_run(document):
    paragraph = document.add_paragraph("Visible start. ")
    paragraph.add_run(HIDDEN).font.hidden = True
    paragraph.add_run(" visible end.")


def hidden_character_style(document):
    document.styles.add_style("Concealed", WD_STYLE_TYPE.CHARACTER).font.hidden = True
    document.add_paragraph().add_run(HIDDEN).style = "Concealed"


def hidden_paragraph_style(document):
    document.styles.add_style("Aside", WD_STYLE_TYPE.PARAGRAPH).font.hidden = True
    document.add_paragraph(HIDDEN, style="Aside")


def hidden_through_a_based_on_chain(document):
    parent = document.styles.add_style("Concealed", WD_STYLE_TYPE.CHARACTER)
    parent.font.hidden = True
    child = document.styles.add_style("ConcealedChild", WD_STYLE_TYPE.CHARACTER)
    child.base_style = parent
    document.add_paragraph().add_run(HIDDEN).style = "ConcealedChild"


def white_text(document):
    document.add_paragraph().add_run(HIDDEN).font.color.rgb = RGBColor(255, 255, 255)


def tiny_text(document):
    document.add_paragraph().add_run(HIDDEN).font.size = Pt(1)


DOCX_TECHNIQUES = [
    pytest.param(hidden_run, "docx_hidden_run", id="hidden-run"),
    pytest.param(hidden_character_style, "docx_hidden_run", id="hidden-character-style"),
    pytest.param(hidden_paragraph_style, "docx_hidden_run", id="hidden-paragraph-style"),
    pytest.param(hidden_through_a_based_on_chain, "docx_hidden_run", id="hidden-based-on-chain"),
    pytest.param(white_text, "text_colour_matches_background", id="white-text"),
    pytest.param(tiny_text, "tiny_font", id="one-point-text"),
]


@pytest.mark.parametrize(("technique", "reason"), DOCX_TECHNIQUES)
def test_a_hidden_docx_run_is_removed_and_reported(technique, reason):
    result = read_resume(docx(technique), DOCX_MIME)

    assert result.hidden.found
    assert result.hidden.reasons == [reason]
    assert result.hidden.samples == [HIDDEN]
    assert result.hidden.chars_removed == len(HIDDEN.replace(" ", ""))
    assert all(word not in result.text for word in HIDDEN.split())
    assert set(BODY) <= set(texts(result))
    assert all(line.page == 1 for line in result.lines)


def test_visible_text_around_a_hidden_run_is_joined_cleanly():
    result = read_resume(docx(hidden_run), DOCX_MIME)

    assert "Visible start. visible end." in texts(result)


def test_a_run_that_says_it_is_not_hidden_beats_a_hidden_style():
    def explicit(document):
        document.styles.add_style("Concealed", WD_STYLE_TYPE.CHARACTER).font.hidden = True
        run = document.add_paragraph().add_run("explicitly shown")
        run.style = "Concealed"
        run.font.hidden = False

    result = read_resume(docx(explicit), DOCX_MIME)

    assert not result.hidden.found
    assert "explicitly shown" in texts(result)


def test_white_text_on_shaded_paragraphs_and_cells_is_legitimate_and_stays():
    def shaded(document):
        paragraph = document.add_paragraph()
        shade(paragraph._p.get_or_add_pPr(), "1F2937")
        paragraph.add_run("white on a shaded paragraph").font.color.rgb = RGBColor(255, 255, 255)
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        shade(cell._tc.get_or_add_tcPr(), "1F2937")
        cell.paragraphs[0].add_run("white in a shaded cell").font.color.rgb = RGBColor(255, 255, 255)

    result = read_resume(docx(shaded), DOCX_MIME)

    assert not result.hidden.found
    assert {"white on a shaded paragraph", "white in a shaded cell"} <= set(texts(result))


def test_text_coloured_like_its_cell_shading_is_hidden():
    def dark_on_dark(document):
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        shade(cell._tc.get_or_add_tcPr(), "111111")
        cell.paragraphs[0].add_run(HIDDEN).font.color.rgb = RGBColor(0, 0, 0)

    result = read_resume(docx(dark_on_dark), DOCX_MIME)

    assert result.hidden.reasons == ["text_colour_matches_background"]
    assert HIDDEN not in result.text


def test_white_text_in_an_unshaded_table_cell_is_kept_because_its_background_is_unknown():
    # A cell's fill can come from a table style or a drawing shape the reader does not resolve. Judging it as white on
    # white would delete a legitimate sidebar, so only plain-page text is judged. ponytail: a known gap.
    def cell_text(document):
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.paragraphs[0].add_run("white in a cell").font.color.rgb = RGBColor(255, 255, 255)

    result = read_resume(docx(cell_text), DOCX_MIME)

    assert not result.hidden.found
    assert "white in a cell" in texts(result)


def test_docx_text_in_tables_comes_in_document_order_and_odd_characters_are_cleaned():
    def build(document):
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).paragraphs[0].add_run("Left cell: Python, Go and SQL")
        table.cell(0, 1).paragraphs[0].add_run("Right cell:\tfinance\u200b team \ufb01rst")
        document.add_paragraph("After the table")

    result = read_resume(docx(build), DOCX_MIME)

    assert texts(result)[len(BODY) :] == [
        "Left cell: Python, Go and SQL",
        "Right cell: finance team first",
        "After the table",
    ]


# --- scanned and unreadable ----------------------------------------------------------------------------------------


def test_a_page_with_almost_no_text_is_scanned_and_returns_no_lines():
    result = read_resume(pdf(write(["Scan of a resume"])), PDF_MIME)

    assert result.scanned and result.lines == [] and result.text == ""


def test_the_scanned_threshold_is_two_hundred_characters_per_page():
    def block(count: int) -> Draw:
        return write(["a" * (count // 2), "a" * (count - count // 2)], size=6)  # small, so it stays on the page

    assert read_resume(pdf(block(MIN_CHARS_PER_PAGE - 1)), PDF_MIME).scanned
    assert not read_resume(pdf(block(MIN_CHARS_PER_PAGE)), PDF_MIME).scanned
    # per page on average: a thin second page does not make a real resume a scan
    assert not read_resume(pdf(write(BODY * 2), write(["Reference available."])), PDF_MIME).scanned
    assert read_resume(pdf(block(MIN_CHARS_PER_PAGE), block(10)), PDF_MIME).scanned


def test_a_short_docx_is_scanned_too():
    result = read_resume(docx(lambda d: d.add_paragraph("short"), body=False), DOCX_MIME)

    assert result.scanned and result.lines == []


def test_hidden_text_is_still_reported_when_the_visible_text_is_too_short():
    result = read_resume(pdf(white_on_white), PDF_MIME)

    assert result.scanned and result.hidden.found


@pytest.mark.parametrize(
    ("data", "mime", "code"),
    [
        pytest.param(b"", PDF_MIME, "corrupt", id="empty-pdf"),
        pytest.param(b"this is not a pdf" * 30, PDF_MIME, "corrupt", id="garbage-pdf"),
        pytest.param(pdf(write(BODY))[:300], PDF_MIME, "corrupt", id="truncated-pdf"),
        pytest.param(
            pdf(write(BODY), encrypt=StandardEncryption("user", "owner")), PDF_MIME, "encrypted", id="locked-pdf"
        ),
        pytest.param(b"", DOCX_MIME, "corrupt", id="empty-docx"),
        pytest.param(b"not a zip file" * 30, DOCX_MIME, "corrupt", id="garbage-docx"),
        pytest.param(
            bytes.fromhex("d0cf11e0a1b11ae1") + bytes(600), DOCX_MIME, "encrypted", id="password-protected-docx"
        ),
    ],
)
def test_an_unreadable_file_raises_with_a_code_and_no_content(data, mime, code):
    with pytest.raises(ResumeUnreadable) as caught:
        read_resume(data, mime)

    assert caught.value.code == code
    assert str(caught.value) == code


def test_a_zip_that_is_not_a_word_document_is_corrupt():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("notes.txt", "hello")

    with pytest.raises(ResumeUnreadable) as caught:
        read_resume(out.getvalue(), DOCX_MIME)

    assert caught.value.code == "corrupt"


def test_a_zip_bomb_is_refused_before_it_is_expanded():
    # A valid resume with one extra part that expands to 51 MB but compresses to a few kilobytes.
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(docx(lambda d: None))) as original,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bomb,
    ):
        for part in original.infolist():
            bomb.writestr(part, original.read(part))
        bomb.writestr("word/media/padding.bin", bytes(51 * 1024 * 1024))

    assert len(out.getvalue()) < 1024 * 1024
    assert read_resume(docx(lambda d: None), DOCX_MIME).lines  # the same resume without the padding reads fine
    with pytest.raises(ResumeUnreadable) as caught:
        read_resume(out.getvalue(), DOCX_MIME)

    assert caught.value.code == "corrupt"


def test_a_pdf_with_too_many_pages_is_refused():
    with pytest.raises(ResumeUnreadable) as caught:
        read_resume(pdf(*[write(["x"])] * 21), PDF_MIME)

    assert caught.value.code == "corrupt"


def test_a_mime_type_other_than_pdf_or_docx_is_a_caller_bug():
    with pytest.raises(ValueError, match="PDF and DOCX"):
        read_resume(b"hello", "text/plain")
