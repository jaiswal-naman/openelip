# Hidden-text test slice

About sixty **invented** resume files, each hiding text in one known way, plus legitimate look-alikes that must not be
flagged. It checks the resume reader (`backend/app/profile/resume/reader.py`, AI_SYSTEM 4.1, requirement REC-06): hidden
text is removed from the extracted text, reported with a reason, and never used.

Names, employers and sentences are made up (a seeded `random.Random(3)`, so a rebuild gives the same text). No real
person's resume is in here, and no generated file is committed.

## Running it

It runs as an ordinary unit test on every pull request. The test builds the files into a temporary directory first:

```
cd backend
uv run pytest tests/profile/test_hidden_text_slice.py -q
```

To look at the files yourself, from the repository root:

```
python evals/resume_extraction/hidden/build_cases.py some_empty_directory
```

That writes the PDFs and DOCX files and a `manifest.json`. The manifest lists, for each file: the technique, the layout,
the reason the reader should report, the hidden strings that must be absent from the extracted text, and the visible lines
that must survive.

## What is in it

| Format | Hiding files | Techniques (each on two layouts, except the covered-glyph pairs and the known gap: one) |
|---|---|---|
| PDF | 37 | tiny font (1, 2, 3 and 3.9 pt); off the page (left, right, below, above); white on white, near-white, white on a white box; light grey (0.9, 0.95); black on a dark box, navy on navy; CMYK white; covered by a visible line, glyph for glyph |
| DOCX | 15 | hidden run (plain, bullets, inside a table cell, in the middle of a paragraph, two runs); hidden through a character style, a paragraph style, a chain of styles; white text; tiny text (1, 2, 3 pt) |

PDF layouts: single column, two columns, a banner over two columns, right-aligned dates, a compact 9 pt page. DOCX
layouts: plain paragraphs, bullets, a two-cell table.

Nine **legitimate look-alikes** must not be flagged and must keep every line: white text on a dark sidebar (three
layouts), a 4 pt footnote, faint grey 0.8 text, a fake-bold double strike, white text on a shaded paragraph and on a
shaded table cell, and a run that says it is visible although its style says hidden.

## The known gap

The PDF **invisible text rendering mode** (text neither filled nor stroked) is not exposed by pdfplumber, so the reader
cannot see it. That one file is marked `xfail(strict=True)`: it must keep failing until a detector exists, and the day
one does, the test goes red and the marker is removed on purpose.

## What it proves, and what it does not

- **Proves:** the code path. Every technique above is caught with its reason, the hidden text is gone, and across all
  files the visible text wrongly removed stays under 0.5% of lines (target from AI_SYSTEM 4.1).
- **Does not prove:** a detection rate on real resumes. The files are built from the same assumptions as the reader, and
  the thresholds (4 pt, contrast 1.5, 200 characters per page) are starting values to tune on the resume gold set. Do
  not quote this slice's pass rate as a quality number.

## Limits the slice does not cover

- Text covered by a *later* opaque shape (a white box painted over black text) is not detected.
- "Covered" means a character that lies over another character's box. A hidden string that runs past the end of the
  text above it keeps its tail; that tail is visible on the page, so it is not hidden.
- DOCX text inside a table cell or a text box with no readable shading is not judged on colour: its fill can come from a
  table style or a drawing shape, and guessing would delete legitimate sidebars.
- Gradient fills are not read. A PDF page with more than 500 filled shapes skips the colour check.
- One gutter, so two columns. Three columns, rotated text and gutters under 10 pt are read row by row.
