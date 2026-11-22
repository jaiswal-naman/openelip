"""Turning a model answer into rows: text and span come from the posting, never from the model (AI_SYSTEM 4.3).

The postings in this file are invented for the tests.
"""

import json

import pytest
from pydantic import ValidationError

from app.ingest.requirements.schema import RequirementItem, RequirementList
from app.ingest.requirements.segment import segments
from app.ingest.requirements.validate import MAX_REQUIREMENTS, output_acceptable, to_rows

# Segments: 1 "Intro", 2 "- Python", 3 "- 3+ years of Python", 4 "- SQL", 5 "- A degree"
POSTING = "Intro\n\n- Python\n- 3+ years of Python\n- SQL\n- A degree"


def item(cites, quote=None, importance="required", kind="skill", min_years=None):
    return RequirementItem(segments=cites, quote=quote, importance=importance, kind=kind, min_years=min_years)


def convert(*items, description=POSTING):
    return to_rows(description, segments(description), RequirementList(requirements=list(items)))


def test_a_quote_gives_the_text_and_span_of_that_occurrence_inside_the_cited_segment():
    (row,) = convert(item([2], "Python")).rows
    assert (row.text, POSTING[row.start : row.end]) == ("Python", "Python")
    assert row.start == POSTING.index("Python")


def test_a_quote_is_searched_only_inside_the_cited_segments():
    description = "Python\nSQL\nPython"
    (row,) = convert(item([3], "Python"), description=description).rows
    assert (row.start, row.end) == (11, 17)  # the third line, not the first


def test_without_a_quote_the_text_runs_from_the_first_to_the_last_cited_segment():
    (row,) = convert(item([2, 3])).rows
    assert row.text == "- Python\n- 3+ years of Python"
    assert POSTING[row.start : row.end] == row.text


def test_the_row_carries_the_importance_and_kind_the_model_chose():
    (row,) = convert(item([5], importance="preferred", kind="education")).rows
    assert (row.importance, row.kind, row.min_years) == ("preferred", "education", None)


def test_a_blank_quote_means_no_quote_and_a_quote_loses_its_edge_whitespace():
    assert convert(item([2], "  ")).rows[0].text == "- Python"
    assert convert(item([2], " Python ")).rows[0].text == "Python"


@pytest.mark.parametrize(
    ("cited", "quote", "reason"),
    [
        ([2], "Java", "quote_not_in_segments"),
        ([2], "python", "quote_not_in_segments"),  # copied exactly: case counts
        ([4], "Python", "quote_not_in_segments"),  # in the posting, but not in the cited segment
        ([99], None, "segment_missing"),
        ([0], None, "segment_missing"),
        ([], None, "segment_missing"),
        ([2, 99], None, "segment_missing"),
        ([2, 4], None, "segments_not_adjacent"),  # 3 lies between them
    ],
)
def test_an_item_that_breaks_a_citation_rule_is_rejected_and_counted(cited, quote, reason):
    done = convert(item(cited, quote))
    assert done.rows == []
    assert done.rejected == {reason: 1}


def test_cited_segments_may_come_in_any_order_and_repeat():
    (row,) = convert(item([3, 2, 2])).rows
    assert row.text == "- Python\n- 3+ years of Python"


def test_min_years_survives_only_when_the_number_is_written_in_the_text():
    kept = convert(item([3], min_years=3))
    assert kept.rows[0].min_years == 3
    assert kept.rejected == {}
    dropped = convert(item([3], min_years=5))
    assert dropped.rows[0].min_years is None
    assert dropped.rejected == {"min_years_dropped": 1}


def test_min_years_must_match_a_whole_number_not_a_digit_of_a_longer_one():
    description = "10 years of Go"
    assert convert(item([1], min_years=1), description=description).rows[0].min_years is None
    assert convert(item([1], min_years=10), description=description).rows[0].min_years == 10


def test_min_years_outside_the_database_range_is_dropped_even_when_written():
    done = convert(item([1], min_years=61), description="61 years of Go")
    assert done.rows[0].min_years is None
    assert done.rejected == {"min_years_dropped": 1}


def test_two_items_with_the_same_span_merge_and_the_first_is_kept():
    done = convert(item([2], importance="required"), item([2], importance="preferred"))
    assert [r.importance for r in done.rows] == ["required"]
    assert done.rejected == {"duplicate": 1}


def test_a_merged_duplicate_does_not_also_count_a_min_years_drop():
    assert convert(item([3]), item([3], min_years=9)).rejected == {"duplicate": 1}


def test_more_than_thirty_valid_items_keep_the_first_thirty_by_position():
    description = "\n".join(f"Skill number {i}" for i in range(35))
    answered_backwards = [item([n]) for n in range(35, 0, -1)]
    done = to_rows(description, segments(description), RequirementList(requirements=answered_backwards))
    assert MAX_REQUIREMENTS == 30
    assert [r.text for r in done.rows] == [f"Skill number {i}" for i in range(30)]
    assert done.rejected == {"over_cap": 5}


def test_ordinals_run_from_one_in_the_order_of_the_posting_not_of_the_answer():
    done = convert(item([5]), item([2]), item([4]))
    assert [(r.ordinal, r.text) for r in done.rows] == [(1, "- Python"), (2, "- SQL"), (3, "- A degree")]


def test_every_kept_row_is_the_posting_at_its_span_after_a_non_bmp_character_too():
    description = "Intro \U0001f600\n- Python\n- 3+ years of Python"
    done = convert(item([2], "Python"), item([2, 3]), item([3], "3+ years"), description=description)
    assert len(done.rows) == 3
    assert all(description[r.start : r.end] == r.text for r in done.rows)
    assert min(r.start for r in done.rows) == 8  # "Intro \U0001f600\n" is eight code points


def test_an_answer_is_unacceptable_only_when_items_were_proposed_and_none_survived():
    assert output_acceptable(convert()) is True  # nothing proposed: a posting may state no requirements
    assert output_acceptable(convert(item([2]))) is True
    assert output_acceptable(convert(item([2]), item([99]))) is True  # a partly valid answer keeps its valid item
    assert output_acceptable(convert(item([99]))) is False


GOOD_ITEM = {"segments": [1], "quote": None, "importance": "required", "kind": "skill", "min_years": None}


@pytest.mark.parametrize(
    "bad_item",
    [
        {**GOOD_ITEM, "text": "invented by the model"},  # an unknown key
        {**GOOD_ITEM, "importance": "mandatory"},
        {**GOOD_ITEM, "kind": "duty"},
        {k: v for k, v in GOOD_ITEM.items() if k != "quote"},  # every key is required
    ],
)
def test_the_schema_rejects_unknown_keys_values_and_missing_keys(bad_item):
    RequirementList.model_validate({"requirements": [GOOD_ITEM]})
    with pytest.raises(ValidationError):
        RequirementList.model_validate({"requirements": [bad_item]})
    with pytest.raises(ValidationError):
        RequirementList.model_validate({"requirements": [GOOD_ITEM], "extra": 1})


def test_the_json_schema_has_no_bounds_and_forbids_extra_keys_everywhere():
    schema = RequirementList.model_json_schema()
    text = json.dumps(schema)
    for bound in ("minimum", "maximum", "minLength", "maxLength", "maxItems", "pattern"):
        assert bound not in text  # AI_SYSTEM 2.3: those rules live in the validator
    objects = [schema, *schema.get("$defs", {}).values()]
    assert all(o["additionalProperties"] is False for o in objects if o.get("type") == "object")
