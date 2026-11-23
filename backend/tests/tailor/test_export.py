"""TLR-03: a resume document renders to PDF and DOCX. The PDF refuses a character it cannot draw instead of dropping it
(decision 27); the DOCX carries every character. Every candidate here is invented.
"""

import io

import docx
import pdfplumber
import pytest

from app.tailor.export import Block, UnsupportedCharacters, render_docx, render_pdf, resume_blocks
from app.tailor.resume import apply_edits, build_base
from tests.tailor.fixtures.invented import bullet, candidate, fact, role


class Edit:
    target_fact_id = "b-python"
    before_text = "Built Python services for payments"
    after_text = "Built and ran Python services for payments"
    supporting_fact_ids = ("b-python",)
    section = "experience"


def doc(facts=None):
    return build_base(candidate() if facts is None else facts, name="Ada Example", email="ada@example.test")


def lines_of(blocks):
    return [text for block in blocks for text in (block.heading, *block.lines, *block.bullets) if text]


def squeeze(text):
    return " ".join(text.split())


def pdf_text(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return " ".join(squeeze(page.extract_text() or "") for page in pdf.pages), len(pdf.pages)


def assert_in_order(text, lines):
    position = 0
    for line in lines:
        found = text.find(squeeze(line), position)
        assert found >= 0, f"missing or out of order: {line!r}"
        position = found + len(squeeze(line))


# ---- the blocks ----------------------------------------


def test_the_blocks_are_the_title_then_each_section_with_its_entries():
    blocks = resume_blocks(doc())
    assert blocks[0] == Block("Ada Example", ("ada@example.test",), (), 0)
    assert [(b.heading, b.level) for b in blocks[1:4]] == [
        ("Experience", 1),
        ("Backend Engineer, Initech Demo", 2),
        ("Data Analyst, Globex Demo", 2),
    ]
    initech = blocks[2]
    assert initech.lines == ("Mar 2019 - Aug 2022",) and initech.bullets[0] == "Built Python services for payments"
    sections = [b.heading for b in blocks if b.level == 1]
    assert sections == ["Experience", "Skills", "Education", "Certifications"]  # no projects: no empty section
    skills = next(b for b in blocks if b.heading == "Skills")
    assert skills.lines == ("Python, SQL, Go",)


def test_a_role_with_no_dates_has_no_date_line_and_a_resume_with_no_name_has_no_title_block():
    blocks = resume_blocks(build_base([fact("r1", "role", "Helper", title="Helper")], name="", email=""))
    assert [b.heading for b in blocks] == ["Experience", "Helper"] and blocks[1].lines == ()


# ---- the PDF ----------------------------------------


def test_the_pdf_holds_every_block_line_in_order():
    blocks = resume_blocks(doc())
    data = render_pdf(blocks)
    text, pages = pdf_text(data)
    assert data.startswith(b"%PDF") and pages == 1
    assert_in_order(text, lines_of(blocks))


def test_the_same_blocks_render_to_the_same_bytes_twice():
    blocks = resume_blocks(doc())
    assert render_pdf(blocks) == render_pdf(blocks)


def test_a_long_resume_runs_onto_more_pages_without_losing_a_line():
    many = [role("r1", "Initech Demo", "Engineer", "2019-01")] + [
        bullet(f"b{n}", f"Delivered item number {n} for the invented payments platform and wrote it up at length", "r1")
        for n in range(80)
    ]
    blocks = resume_blocks(doc(many))
    text, pages = pdf_text(render_pdf(blocks))
    assert pages >= 2
    assert_in_order(text, lines_of(blocks))


def test_markup_characters_in_a_line_are_text_not_markup():
    blocks = [Block("R&D <lead> & more", ("a < b and c > d",), ("Fixed the &amp; bug",))]
    text, _ = pdf_text(render_pdf(blocks))
    assert_in_order(text, lines_of(blocks))


def test_letters_of_latin_1_are_drawn_and_the_edge_of_latin_1_is_the_edge_of_the_pdf():
    text, _ = pdf_text(render_pdf([Block("Café ÿ", ("naïve",))]))
    assert "Café" in text and "naïve" in text
    with pytest.raises(UnsupportedCharacters):
        render_pdf([Block("Edge", ("Ā",))])  # U+0100 is the first character above Latin-1


@pytest.mark.parametrize("where", ["heading", "line", "bullet"])
def test_a_character_outside_latin_1_is_refused_and_named_never_dropped(where):
    block = {
        "heading": Block("Ada – Example"),
        "line": Block("Fine", ("Mar 2019 – Aug 2022",)),
        "bullet": Block("Fine", (), ("Cut costs by €5",)),
    }[where]
    with pytest.raises(UnsupportedCharacters) as refused:
        render_pdf([Block("Clean"), block])
    assert set(refused.value.characters) <= {"–", "€"} and refused.value.characters
    assert "Mar 2019" not in str(refused.value) and "Cut costs" not in str(refused.value)  # no resume text in the message


# ---- the DOCX ----------------------------------------


def paragraphs(data):
    return [(p.style.name, p.text) for p in docx.Document(io.BytesIO(data)).paragraphs]


def test_the_docx_has_headings_plain_lines_and_bullet_paragraphs():
    found = paragraphs(render_docx(doc()))
    assert found[0] == ("Title", "Ada Example") and ("Normal", "ada@example.test") in found
    assert ("Heading 1", "Experience") in found and ("Heading 2", "Backend Engineer, Initech Demo") in found
    assert ("Normal", "Mar 2019 - Aug 2022") in found
    assert ("List Bullet", "Built Python services for payments") in found
    assert ("Normal", "Python, SQL, Go") in found


def test_the_docx_holds_every_block_line_in_order():
    blocks = resume_blocks(doc())
    text = " ".join(squeeze(t) for _, t in paragraphs(render_docx(doc())))
    assert_in_order(text, lines_of(blocks))


def test_the_docx_carries_the_characters_the_pdf_cannot():
    wide = doc([role("r1", "Initech Demo", "Engineer", "2019-01"), bullet("b1", "Cut costs by €5 – a lot", "r1")])
    with pytest.raises(UnsupportedCharacters):
        render_pdf(resume_blocks(wide))
    assert ("List Bullet", "Cut costs by €5 – a lot") in paragraphs(render_docx(wide))


def test_control_characters_do_not_break_the_docx():
    odd = doc([role("r1", "Initech Demo", "Engineer", "2019-01"), bullet("b1", "Built\x00 the\x0b thing", "r1")])
    assert ("List Bullet", "Built the thing") in paragraphs(render_docx(odd))


# ---- both formats from an edited document ----------------------------------------


def test_an_edit_shows_in_both_formats_and_the_old_words_do_not():
    edited = apply_edits(doc(), [Edit()])
    blocks = resume_blocks(edited)
    text, _ = pdf_text(render_pdf(blocks))
    assert "Built and ran Python services for payments" in text and "Built Python services for payments" not in text
    docx_text = " ".join(t for _, t in paragraphs(render_docx(edited)))
    assert "Built and ran Python services for payments" in docx_text
    assert "Built Python services for payments" not in docx_text


def test_a_document_with_only_a_name_renders_in_both_formats():
    only_name = build_base([], name="Ada Example", email="")
    assert pdf_text(render_pdf(resume_blocks(only_name)))[0] == "Ada Example"
    assert paragraphs(render_docx(only_name)) == [("Title", "Ada Example")]


def test_blocks_made_by_another_module_render_like_any_other():
    packet = [Block("Cover note", ("I build Python services.",)), Block("Questions", (), ("Why this role? Because.",))]
    assert_in_order(pdf_text(render_pdf(packet))[0], lines_of(packet))
