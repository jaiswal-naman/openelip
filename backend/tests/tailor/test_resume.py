"""TLR-01, TLR-03: the base resume is built by code from usable facts, and approved edits change it without touching the
input. Every candidate here is invented (tests/tailor/fixtures/invented.py).
"""

from dataclasses import dataclass, field

import pytest

from app.tailor.resume import (
    Bullet,
    ResumeDoc,
    apply_edits,
    build_base,
    derive_op,
    line_text,
)
from tests.tailor.fixtures.invented import EDIT, bullet, candidate, fact, role, skill


@dataclass(frozen=True)
class Edit:
    """Anything with the stored shape of an edit will do (a resume_edits row, a VerifiedEdit)."""

    target_fact_id: str | None = None
    before_text: str = ""
    after_text: str = ""
    supporting_fact_ids: tuple[str, ...] = field(default_factory=tuple)
    section: str = "experience"


def base(facts=None):
    return build_base(candidate() if facts is None else facts, name="Ada Example", email="ada@example.test")


def texts(bullets):
    return [b.text for b in bullets]


# ---- the base document ----------------------------------------


def test_roles_are_newest_first_and_a_role_with_no_start_date_comes_last():
    facts = [
        role("r-old", "Old Demo", "Clerk", "2010-01", "2012-01"),
        fact("r-undated", "role", "Undated Demo | Helper", employer="Undated Demo", title="Helper"),
        role("r-new", "New Demo", "Lead", "2021-05"),
        role("r-mid", "Mid Demo", "Analyst", "2015-06", "2020-12"),
    ]
    assert [b.fact_id for b in base(facts).roles] == ["r-new", "r-mid", "r-old", "r-undated"]


def test_a_role_header_and_dates_come_from_its_attributes_not_from_its_statement():
    facts = [
        fact(
            "r1",
            "role",
            "SENIOR DIRECTOR OF EVERYTHING | Elsewhere Inc\nJanuary 1990 - Present",
            employer="Initech Demo",
            title="Backend Engineer",
            start_date="2019-03",
            end_date="2022-08",
        )
    ]
    (block,) = base(facts).roles
    assert (block.header, block.dates) == ("Backend Engineer, Initech Demo", "Mar 2019 - Aug 2022")


@pytest.mark.parametrize(
    ("attributes", "dates"),
    [
        ({"start_date": "2019-03", "end_date": "2022"}, "Mar 2019 - 2022"),
        ({"start_date": "2019", "is_current": True}, "2019 - Present"),
        ({"start_date": "2019-03"}, "Mar 2019"),  # no end and not current: Present is never guessed
        ({"end_date": "2022-08"}, "Aug 2022"),  # a lone date is printed as it is, never given a made-up partner
        ({"is_current": True}, "Present"),
        ({"start_date": "2019-03", "end_date": "2022-08", "is_current": True}, "Mar 2019 - Aug 2022"),
        ({}, ""),
        ({"start_date": "2019-13", "end_date": "soon"}, ""),  # a value that is not a date is not printed
    ],
)
def test_a_missing_date_is_left_out_and_present_is_printed_only_when_the_role_is_current(attributes, dates):
    (block,) = base([fact("r1", "role", "Initech Demo | Engineer", employer="Initech Demo", **attributes)]).roles
    assert block.dates == dates


def test_a_role_whose_attributes_hold_no_name_shows_the_candidates_own_words():
    (block,) = base([fact("r1", "role", "Freelance work\n2020 - 2021")]).roles  # a corrected fact has no attributes
    assert block.header == "Freelance work 2020 - 2021" and block.dates == ""


def test_achievements_sit_under_their_role_in_given_order_and_every_bullet_carries_its_fact_id():
    doc = base()
    initech, globex = doc.roles
    assert (initech.fact_id, globex.fact_id) == ("role-initech", "role-globex")
    assert [(b.fact_id, b.text) for b in initech.bullets] == [
        ("b-python", "Built Python services for payments"),
        ("b-report", "Cut report build time by 30%"),
        ("b-checkout", "Contributed to the checkout migration"),
        ("b-mentor", "Mentored one junior developer"),
    ]
    assert [b.fact_id for b in globex.bullets] == ["b-sql", "b-invoice"]


def test_skills_education_certifications_and_projects_get_their_own_sections():
    facts = candidate() + [fact("p1", "project", "Project: Ledger Sync (2024)\nLaunched a side project.")]
    doc = base(facts)
    assert [(b.fact_id, b.text) for b in doc.skills] == [("s-python", "Python"), ("s-sql", "SQL"), ("s-go", "Go")]
    assert [(b.fact_id, b.text) for b in doc.education] == [
        ("edu-lakeshore", "B.Sc. Computer Science, Lakeshore Technical College, 2012 - 2016")
    ]
    assert [(b.fact_id, b.text) for b in doc.certifications] == [("cert-scrum", "Certified Scrum Master 2018")]
    assert [(b.fact_id, b.text) for b in doc.projects] == [
        ("p1", "Project: Ledger Sync (2024) Launched a side project.")
    ]
    assert (doc.name, doc.email) == ("Ada Example", "ada@example.test")


def test_a_chat_stated_fact_is_not_in_the_base_but_a_hand_added_or_corrected_one_is():
    facts = candidate() + [
        skill("s-hand", "Terraform", source=EDIT),
        bullet("b-corrected", "Fixed a typo the candidate corrected", "role-initech", source=EDIT),
    ]
    doc = base(facts)
    assert "s-quill" not in [b.fact_id for b in doc.skills]  # said in chat: it can only arrive through add_from_fact
    assert "s-hand" in [b.fact_id for b in doc.skills]  # said by hand in the record: it is the candidate's own record
    assert "b-corrected" in [b.fact_id for b in doc.roles[0].bullets]


def test_an_achievement_whose_role_is_not_on_the_resume_is_kept_in_an_other_experience_block():
    facts = [
        role("r1", "Initech Demo", "Engineer", "2020-01"),
        bullet("b1", "Kept", "r1"),
        bullet("b2", "Orphan", "gone"),
    ]
    doc = base(facts)
    assert [b.fact_id for b in doc.roles] == ["r1", None]
    other = doc.roles[1]
    assert (other.header, other.dates, texts(other.bullets)) == ("Other experience", "", ["Orphan"])


def test_a_fact_of_another_kind_is_not_in_the_base():
    facts = [fact("o1", "other", "A summary the base does not show"), skill("s1", "Python")]
    doc = base(facts)
    assert doc.roles == () and [b.fact_id for b in doc.skills] == ["s1"]


@pytest.mark.parametrize(
    ("statement", "text"),
    [
        ("- Built the pipeline", "Built the pipeline"),
        ("•   Built the pipeline", "Built the pipeline"),
        (
            "- Negotiated the reporting pipeline saving about 200 hours a\nyear",
            "Negotiated the reporting pipeline saving about 200 hours a year",
        ),
        ("-5% churn", "-5% churn"),  # a minus sign is not a bullet mark
        ("Built\x00 the\x0bpipeline", "Built the pipeline"),
        ("  spaced \t out  ", "spaced out"),
    ],
)
def test_a_statement_becomes_one_clean_line(statement, text):
    assert line_text(statement) == text


# ---- applying edits ----------------------------------------


def test_a_rephrase_replaces_exactly_one_bullet():
    before = base()
    after = apply_edits(
        before,
        [
            Edit(
                "b-python",
                "Built Python services for payments",
                "Built and ran Python services for payments",
                ("b-python",),
            )
        ],
    )
    assert texts(after.roles[0].bullets) == [
        "Built and ran Python services for payments",
        "Cut report build time by 30%",
        "Contributed to the checkout migration",
        "Mentored one junior developer",
    ]
    assert after.roles[1] == before.roles[1] and after.skills == before.skills  # nothing else moved


def test_a_reorder_permutes_one_roles_bullets_in_the_order_the_edit_lists():
    new_order = ("b-report", "b-python", "b-mentor", "b-checkout")
    edit = Edit(None, "x\ny", "y\nx", new_order)  # equal lines in another order: a reorder; the ids are the new order
    after = apply_edits(base(), [edit])
    assert [b.fact_id for b in after.roles[0].bullets] == list(new_order)
    assert [b.fact_id for b in after.roles[1].bullets] == ["b-sql", "b-invoice"]


def test_a_reorder_permutes_the_skills_line():
    after = apply_edits(base(), [Edit(None, "a\nb\nc", "c\nb\na", ("s-go", "s-sql", "s-python"), "skills")])
    assert texts(after.skills) == ["Go", "SQL", "Python"]


def test_a_remove_deletes_one_bullet_or_one_skill():
    after = apply_edits(
        base(),
        [
            Edit("b-mentor", "Mentored one junior developer", "", ("b-mentor",)),
            Edit("s-sql", "SQL", "", ("s-sql",), "skills"),
        ],
    )
    assert [b.fact_id for b in after.roles[0].bullets] == ["b-python", "b-report", "b-checkout"]
    assert texts(after.skills) == ["Python", "Go"]


def test_an_add_from_fact_appends_the_facts_statement_to_its_kinds_section():
    quill = Edit(None, "", "Quillscript", ("s-quill",), "skills")
    after = apply_edits(base(), [quill])
    assert [(b.fact_id, b.text) for b in after.skills][-1] == ("s-quill", "Quillscript")
    cert = Edit(None, "", "Certified Kubernetes Demo", ("c-new",), "certifications")
    assert [b.fact_id for b in apply_edits(base(), [cert]).certifications] == ["cert-scrum", "c-new"]


def test_adding_a_fact_that_is_already_on_the_resume_changes_nothing():
    already = Edit(None, "", "Python", ("s-python",), "skills")
    assert apply_edits(base(), [already]) == base()


def test_applying_edits_returns_a_new_document_and_leaves_the_input_alone():
    original = base()
    snapshot = repr(original)
    result = apply_edits(original, [Edit("b-python", "x", "Changed", ("b-python",)), Edit("s-go", "Go", "", ("s-go",))])
    assert result is not original and repr(original) == snapshot
    assert (
        isinstance(result, ResumeDoc)
        and isinstance(result.skills, tuple)
        and isinstance(result.roles[0].bullets, tuple)
    )


def test_edits_are_applied_in_the_order_given():
    one = Edit("b-python", "Built Python services for payments", "First", ("b-python",))
    two = Edit("b-python", "First", "Second", ("b-python",))
    assert apply_edits(base(), [one, two]).roles[0].bullets[0] == Bullet("b-python", "Second")


def test_an_edit_whose_fact_is_no_longer_on_the_resume_is_skipped_so_an_export_drops_it():
    gone = Edit("b-superseded", "x", "Changed", ("b-superseded",))
    stale_reorder = Edit(None, "a\nb", "b\na", ("b-python", "b-superseded"))  # the set no longer matches any group
    assert apply_edits(base(), [gone, stale_reorder, Edit("s-ghost", "x", "", ("s-ghost",))]) == base()


# ---- the op an edit row stands for ----------------------------------------


@pytest.mark.parametrize(
    ("target", "before", "after", "op"),
    [
        ("f1", "old words", "new words", "rephrase"),
        (None, "", "A skill", "add_from_fact"),
        ("f1", "A bullet", "", "remove"),
        (None, "a\nb\nc", "c\na\nb", "reorder"),
        (None, "a\nb", "a\nb\nc", "rephrase"),  # not equal multisets of lines: not a reorder
        ("f1", "a\nb", "b\na", "rephrase"),  # a target means one fact is reworded
        ("f1", "", "", "add_from_fact"),  # the rule's order: before_text first
    ],
)
def test_the_op_of_a_stored_edit_is_derived_by_the_documented_rule(target, before, after, op):
    assert derive_op(target, before, after) == op
