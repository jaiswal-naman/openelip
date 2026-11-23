"""REC-01 and REC-07 at extraction: the model only points, and code decides what becomes a fact (plan 03-03, task 1).

Each test is one behaviour from the plan. The resumes are a few invented lines; the results are built straight from
the reader's dataclasses (fake.result_from_lines), so nothing here depends on how a file is read.
"""

import pytest
from pydantic import ValidationError

from app.profile.extract import validate
from app.profile.extract.fake import result_from_lines
from app.profile.extract.schemas import EducationSkillItem, RoleItem, StatementItem
from app.profile.extract.validate import check_education_skills, check_roles, check_statements


def doc(*lines):
    return result_from_lines(list(lines))


def role(start=1, end=1, **fields):
    base = {
        "kind": "role", "line_start": start, "line_end": end, "parent": None, "employer": None, "title": None,
        "start_date": None, "end_date": None, "is_current": None,
    }  # fmt: skip
    return RoleItem(**{**base, **fields})


def achievement(line, parent):
    return role(line, line, kind="achievement", parent=parent)


def study(start=1, end=1, **fields):
    base = {
        "kind": "education", "line_start": start, "line_end": end, "institution": None, "degree": None, "name": None,
        "start_date": None, "end_date": None,
    }  # fmt: skip
    return EducationSkillItem(**{**base, **fields})


def reasons(checked):
    return [u.reason for u in checked.unverified]


# --- lines and copying -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("start, end", [(0, 1), (2, 9), (3, 2), (-1, -1)])
def test_lines_outside_the_resume_are_unverified_bad_lines(start, end):
    result = doc("Engineer, Globex", "Mar 2021 - Present")
    checked = check_roles([role(start, end, employer="Globex")], result)
    assert checked.kept == []
    assert [(u.reason, u.source_span, u.source_quote) for u in checked.unverified] == [("bad_lines", None, None)]


def test_statement_and_quote_are_the_cited_text_not_the_models():
    result = doc("Header", "Data Engineer, Globex Corp", "Mar 2021 - Present", "Built things")
    item = role(2, 3, employer="Globex Corp", title="Data Engineer", start_date="Mar 2021", end_date="Present")
    (fact,) = check_roles([item], result).kept
    first = len("Header\n")
    assert fact.source_span == (first, first + len("Data Engineer, Globex Corp\nMar 2021 - Present"))
    assert fact.statement == fact.source_quote == result.text[fact.source_span[0] : fact.source_span[1]]
    assert fact.source_quote == "Data Engineer, Globex Corp\nMar 2021 - Present"


def test_the_schemas_refuse_a_statement_quote_or_duration_from_the_model():
    base = {
        "kind": "role", "line_start": 1, "line_end": 1, "parent": None, "employer": None, "title": None,
        "start_date": None, "end_date": None, "is_current": None,
    }  # fmt: skip
    for extra in ({"statement": "x"}, {"source_quote": "x"}, {"duration_months": 12}):
        with pytest.raises(ValidationError):
            RoleItem(**base, **extra)
    with pytest.raises(ValidationError):
        StatementItem(line_start=1, line_end=1, text="made up")


def test_a_long_citation_is_unverified_because_the_statement_would_not_fit_the_table():
    result = doc("x" * (validate.MAX_STATEMENT + 1))
    checked = check_roles([role(1, 1, employer="x")], result)
    assert checked.kept == [] and reasons(checked) == ["statement_too_long"]


# --- names in the cited text -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field, reason", [("employer", "employer_not_in_text"), ("title", "title_not_in_text")])
def test_a_role_name_that_is_not_in_the_cited_text_is_unverified(field, reason):
    result = doc("Data Engineer, Globex Corp", "Mar 2021 - Present")
    item = role(1, 2, **{"employer": "Globex Corp", "title": "Data Engineer", field: "Initech"})
    checked = check_roles([item], result)
    assert checked.kept == [] and reasons(checked) == [reason]
    assert checked.unverified[0].source_quote == result.text  # the candidate is shown what was cited


@pytest.mark.parametrize(
    "field, reason", [("institution", "institution_not_in_text"), ("degree", "degree_not_in_text")]
)
def test_an_education_name_that_is_not_in_the_cited_text_is_unverified(field, reason):
    result = doc("B.Sc. Mathematics, Eastvale Polytechnic, 2016")
    item = study(
        **{"institution": "Eastvale Polytechnic", "degree": "B.Sc. Mathematics", field: "Elsewhere University"}
    )
    checked = check_education_skills([item], result)
    assert checked.kept == [] and reasons(checked) == [reason]


def test_a_name_matches_after_case_and_whitespace_normalisation_and_is_kept_as_written():
    result = doc("Senior Data  Engineer", "GLOBEX   Corp, 2020 - 2022")
    item = role(1, 2, employer="globex corp", title="senior data engineer", start_date="2020", end_date="2022")
    (fact,) = check_roles([item], result).kept
    assert fact.attributes["employer"] == "GLOBEX Corp" and fact.attributes["title"] == "Senior Data Engineer"


def test_a_name_must_be_a_whole_word_in_the_text():
    result = doc("Engineering Manager, Globexion")
    checked = check_roles([role(1, 1, employer="Globex", title="Engineer")], result)
    assert checked.kept == [] and reasons(checked) == ["employer_not_in_text"]


def test_a_role_with_neither_employer_nor_title_is_unverified():
    checked = check_roles([role(1, 1)], doc("Mar 2021 - Present"))
    assert checked.kept == [] and reasons(checked) == ["role_without_details"]
    assert reasons(check_education_skills([study(1, 1)], doc("Mar 2021"))) == ["education_without_details"]


# --- dates -------------------------------------------------------------------------------------------------------


def test_dates_must_run_forward():
    backwards = check_roles(
        [role(1, 2, employer="Globex", start_date="2021-03", end_date="2019-01")],
        doc("Engineer, Globex", "2021-03 - 2019-01"),
    )
    assert backwards.kept == [] and reasons(backwards) == ["dates_backwards"]
    forward = check_roles(
        [role(1, 2, employer="Globex", start_date="2019-01", end_date="2021-03")],
        doc("Engineer, Globex", "2019-01 - 2021-03"),
    )
    assert len(forward.kept) == 1 and forward.unverified == []


@pytest.mark.parametrize(
    "start, end, forward", [("2019", "Mar 2019", True), ("Mar 2019", "2019", True), ("Jun 2019", "Mar 2019", False)]
)
def test_a_year_without_a_month_is_early_at_a_start_and_late_at_an_end(start, end, forward):
    result = doc("Engineer, Globex", f"{start} - {end}")
    checked = check_roles([role(1, 2, employer="Globex", start_date=start, end_date=end)], result)
    assert bool(checked.kept) is forward
    assert reasons(checked) == ([] if forward else ["dates_backwards"])


def test_a_date_that_is_not_in_the_cited_text_is_unverified():
    result = doc("Engineer, Globex", "Mar 2021 - Present")
    checked = check_roles([role(1, 2, employer="Globex", start_date="Jun 2020")], result)
    assert checked.kept == [] and reasons(checked) == ["date_not_in_text"]


def test_a_date_that_does_not_parse_is_unverified():
    result = doc("Engineer, Globex", "Spring 2021 - Present")
    checked = check_roles([role(1, 2, employer="Globex", start_date="Spring 2021")], result)
    assert checked.kept == [] and reasons(checked) == ["date_unparseable"]


@pytest.mark.parametrize(
    "written, expected",
    [
        ("2021-03", (2021, 3)), ("2021/3", (2021, 3)), ("03/2021", (2021, 3)), ("Mar 2021", (2021, 3)),
        ("March 2021", (2021, 3)), ("Sept. 2021", (2021, 9)), ("2021", (2021, None)),
        ("Spring 2021", None), ("13/2021", None), ("2021-00", None), ("1850", None), ("Marz 2021", None),
    ],
)  # fmt: skip
def test_the_date_formats_that_parse(written, expected):
    assert validate._parse_date(written) == expected


def test_durations_are_counted_by_code_in_whole_months_both_ends_included():
    month = check_roles(
        [role(1, 2, employer="Globex", start_date="Mar 2021", end_date="Aug 2021")],
        doc("Globex", "Mar 2021 - Aug 2021"),
    )
    year = check_roles(
        [role(1, 2, employer="Globex", start_date="2018", end_date="2020")], doc("Globex", "2018 - 2020")
    )
    assert month.kept[0].attributes["duration_months"] == 6
    assert year.kept[0].attributes["duration_months"] == 36
    one_end = check_roles([role(1, 2, employer="Globex", start_date="Mar 2021")], doc("Globex", "Mar 2021"))
    assert "duration_months" not in one_end.kept[0].attributes


def test_a_role_that_runs_to_present_is_current_and_has_no_end_date():
    result = doc("Engineer, Globex", "Mar 2021 - Present")
    (fact,) = check_roles([role(1, 2, employer="Globex", start_date="Mar 2021", end_date="Present")], result).kept
    assert fact.attributes["is_current"] is True and "end_date" not in fact.attributes
    assert fact.attributes["start_date"] == "2021-03"


def test_a_claim_of_current_with_no_such_word_in_the_text_is_not_believed():
    result = doc("Engineer, Globex", "Mar 2021 - Aug 2023")
    (fact,) = check_roles([role(1, 2, employer="Globex", start_date="Mar 2021", is_current=True)], result).kept
    assert "is_current" not in fact.attributes


# --- duplicates, parents, overlaps -------------------------------------------------------------------------------


def test_exact_duplicates_merge_and_a_child_follows_its_twin():
    result = doc("Engineer, Globex", "2020 - 2021", "Cut costs by 10%")
    twin = {"employer": "Globex", "title": "Engineer", "start_date": "2020", "end_date": "2021"}
    checked = check_roles([role(1, 2, **twin), role(1, 2, **twin), achievement(3, parent=1)], result)
    assert [f.kind for f in checked.kept] == ["role", "achievement"]
    assert checked.kept[1].parent_index == 0


def test_an_achievement_points_at_its_role_in_the_kept_list():
    result = doc("Engineer, Globex", "Cut costs by 10%", "Analyst, Initech", "Wrote the weekly report")
    items = [
        role(1, 1, employer="Globex"), role(3, 3, employer="Initech"),
        achievement(2, parent=0), achievement(4, parent=1),
    ]  # fmt: skip
    kept = check_roles(items, result).kept
    assert [(f.kind, f.parent_index) for f in kept] == [
        ("role", None),
        ("role", None),
        ("achievement", 0),
        ("achievement", 1),
    ]


@pytest.mark.parametrize("parent", [7, -1, 1])  # past the end, negative, an achievement rather than a role
def test_an_achievement_with_a_parent_that_is_not_a_role_is_unverified(parent):
    result = doc("Engineer, Globex", "Cut costs by 10%", "Wrote the report")
    items = [role(1, 1, employer="Globex"), achievement(2, parent=0), achievement(3, parent=parent)]
    checked = check_roles(items, result)
    assert reasons(checked) == ["bad_parent"]
    assert [f.kind for f in checked.kept] == ["role", "achievement"]  # the achievement with a real parent stays


def test_an_achievement_whose_role_could_not_be_verified_is_not_kept():
    result = doc("Engineer, Globex", "Cut costs by 10%")
    checked = check_roles([role(1, 1, employer="Initech"), achievement(2, parent=0)], result)
    assert checked.kept == [] and sorted(reasons(checked)) == ["employer_not_in_text", "parent_not_kept"]


def test_overlapping_roles_are_flagged_not_dropped():
    result = doc(
        "Engineer, Globex",
        "Jan 2020 - Dec 2021",
        "Analyst, Initech",
        "Jun 2021 - Present",
        "Clerk, Hooli",
        "2010 - 2012",
    )
    items = [
        role(1, 2, employer="Globex", start_date="Jan 2020", end_date="Dec 2021"),
        role(3, 4, employer="Initech", start_date="Jun 2021", end_date="Present"),
        role(5, 6, employer="Hooli", start_date="2010", end_date="2012"),
    ]  # fmt: skip
    kept = check_roles(items, result).kept
    assert [f.attributes.get("flags") for f in kept] == [["overlaps_other_role"], ["overlaps_other_role"], None]


def test_roles_that_only_touch_in_a_month_are_not_flagged():
    result = doc("Engineer, Globex", "Jan 2020 - Mar 2021", "Analyst, Initech", "Mar 2021 - Present")
    items = [
        role(1, 2, employer="Globex", start_date="Jan 2020", end_date="Mar 2021"),
        role(3, 4, employer="Initech", start_date="Mar 2021", end_date="Present"),
    ]  # fmt: skip
    assert all("flags" not in f.attributes for f in check_roles(items, result).kept)


# --- protected characteristics (REC-07) --------------------------------------------------------------------------


def test_a_cited_line_that_states_a_protected_characteristic_is_dropped_and_counted():
    result = doc("Marital status: single", "Engineer, Globex")
    items = [role(1, 1, employer="Globex", title="Engineer"), role(2, 2, employer="Globex", title="Engineer")]
    checked = check_roles(items, result)
    assert checked.protected_skipped == 1  # counted, and not shown as an item that failed either
    assert checked.unverified == [] and [f.attributes["employer"] for f in checked.kept] == ["Globex"]


def test_a_summary_line_that_states_a_protected_characteristic_is_dropped_and_counted():
    result = doc("Date of birth: 04/03/1998", "Backend engineer who likes small services.")
    items = [StatementItem(line_start=1, line_end=1), StatementItem(line_start=2, line_end=2)]
    checked = check_statements(items, result)
    assert checked.protected_skipped == 1
    assert [(f.kind, f.statement) for f in checked.kept] == [("other", "Backend engineer who likes small services.")]


# --- skills, certifications ----------------------------------------------------------------------------------


def test_a_skill_fact_is_the_skill_in_the_cited_line_not_the_whole_line():
    result = doc("Skills: Django, Go, SQL")
    item = study(kind="skill", name="go")
    (fact,) = check_education_skills([item], result).kept
    start = result.text.index(", Go") + 2
    assert fact.source_span == (start, start + 2) and fact.statement == fact.source_quote == "Go"
    assert fact.attributes == {"name": "Go"}


def test_a_skill_that_is_only_part_of_another_word_is_not_in_the_text():
    checked = check_education_skills([study(kind="skill", name="Go")], doc("Skills: Django"))
    assert checked.kept == [] and reasons(checked) == ["name_not_in_text"]
    assert reasons(check_education_skills([study(kind="skill")], doc("Skills: Django"))) == ["name_missing"]


def test_a_certification_keeps_its_name_and_date_as_found_in_the_text():
    result = doc("Certified Scrum Master, 2022")
    (fact,) = check_education_skills(
        [study(kind="certification", name="certified scrum master", end_date="2022")], result
    ).kept
    assert fact.attributes == {"name": "Certified Scrum Master", "end_date": "2022"}
    assert fact.source_quote == "Certified Scrum Master, 2022"


def test_the_education_dates_and_names_become_attributes():
    result = doc("B.Sc. Mathematics, Eastvale Polytechnic, 2012 - 2016")
    item = study(institution="Eastvale Polytechnic", degree="B.Sc. Mathematics", start_date="2012", end_date="2016")
    (fact,) = check_education_skills([item], result).kept
    assert fact.kind == "education"
    assert fact.attributes == {
        "institution": "Eastvale Polytechnic", "degree": "B.Sc. Mathematics", "start_date": "2012", "end_date": "2016",
        "duration_months": 60,
    }  # fmt: skip
