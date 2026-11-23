"""Splitting a posting into numbered segments (AI_SYSTEM 4.3). The postings in this file are invented for the tests."""

from app.ingest.requirements.segment import Segment, segments


def test_one_numbered_segment_per_non_blank_line():
    found = segments("Intro\n\n- Python\n- 3+ years")
    assert found == [Segment(1, 0, 5, "Intro"), Segment(2, 7, 15, "- Python"), Segment(3, 16, 26, "- 3+ years")]


def test_every_span_is_the_segment_text_and_leaves_the_edge_whitespace_out():
    description = "  Python  \n\t\n SQL\t\r\n"
    found = segments(description)
    assert [s.text for s in found] == ["Python", "SQL"]
    assert all(description[s.start : s.end] == s.text for s in found)


def test_offsets_count_code_points_so_a_non_bmp_character_does_not_shift_them():
    description = "Intro \U0001f600\nPython"  # the emoji is one code point and two UTF-16 units
    _, second = segments(description)
    assert (second.start, second.end) == (8, 14)
    assert description[second.start : second.end] == "Python"


def test_a_carriage_return_ends_a_line_too():
    assert [s.text for s in segments("a\r\nb\rc")] == ["a", "b", "c"]


def test_a_posting_with_no_text_has_no_segments():
    assert segments("") == []
    assert segments(" \n\t\n") == []
