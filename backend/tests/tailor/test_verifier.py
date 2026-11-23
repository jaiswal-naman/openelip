"""TLR-02, CHT-04, invariant 3: layer 1 of the tailoring verifier. Code checks each proposed edit against the facts that
support it, in a fixed order, and names why it discards one. Every candidate and requirement here is invented.

Aliases follow tests/tailor/fixtures/invented.py: f1 the Initech role, f2 "Built Python services for payments", f3 "Cut
report build time by 30%", f4 "Contributed to the checkout migration", f5 "Mentored one junior developer", f6 the
Globex role, f7 and f8 its bullets, f9 to f11 the skills Python, SQL and Go, f12 the degree, f13 the certification and
f14 Quillscript, a skill the candidate stated in chat (confirmed, not on the resume).
"""

import json
from pathlib import Path

import pytest

from app.tailor import pipeline
from app.tailor.schemas import EditItem
from app.tailor.verifier import REASONS, Requirement, build_context, check, scan, unsupported_reason
from tests.tailor.fixtures.invented import EDIT, bullet, candidate, requirements, role, skill

BUILT = "Built Python services for payments"
SEVEN = json.loads((Path(__file__).parent / "fixtures" / "seven_reasons.json").read_text(encoding="utf-8"))


def edit(op="rephrase", target=None, after="", supporting=(), order=(), section="experience"):
    return EditItem(
        op=op,
        target=target,
        section=section,
        after_text=after,
        supporting=list(supporting),
        requirements=[],
        order=list(order),
    )


def reason(item, facts=None, reqs=None):
    ctx = build_context(candidate() if facts is None else facts, requirements() if reqs is None else reqs)
    return check(item, ctx)


def rephrase(after, *, target="f2", supporting=("f2",), facts=None, reqs=None):
    return reason(edit("rephrase", target, after, supporting), facts, reqs)


# ---- the scanner ----------------------------------------


def test_numbers_dates_and_entity_shaped_tokens_are_told_apart():
    found = scan("Cut costs 2,000 times by 40% since March 2021 using S3, C++ and Node.js; ran 3D jobs for two teams.")
    assert found.numbers == {"2000", "40", "2"}  # "3D" is a token with a digit, not the number 3
    assert found.dates == ((2021, 3),)
    assert {"s3", "c++", "node.js", "3d"} <= set(found.shapes)


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("two teams and a dozen reports", {"2", "12"}),
        ("1.50 and 1,250.5 and 007", {"1.5", "1250.5", "7"}),
        ("thirty percent, hundred, thousand, million", {"30", "100", "1000", "1000000"}),
        ("Q3 2021 and FY2024 and 10x and 2M", set()),  # a digit inside a word is part of that word
    ],
)
def test_numbers_are_compared_by_value_and_number_words_count(text, numbers):
    assert scan(text).numbers == numbers


def test_a_year_is_a_date_and_not_a_number_and_a_month_belongs_to_the_year_it_precedes():
    found = scan("From Sept. 2019 to 2021, and May 2020, and in 1998.")
    assert found.dates == ((2019, 9), (2021, None), (2020, 5), (1998, None))
    assert found.numbers == set()
    assert "sept" not in found.shapes and "may" not in found.shapes  # a month that dates something is not an entity


def test_a_capitalised_word_is_an_entity_unless_it_starts_a_sentence():
    assert scan("Built Python services. Shipped Go tools.").shapes == ("python", "go")
    assert scan("Wrote the Report. Then the Review.").shapes == ("report", "review")  # "Then" starts a sentence


def test_acronyms_and_mixed_case_words_are_entities_even_at_the_start_of_a_sentence():
    assert set(scan("AWS runs it. JavaScript ships. iOS builds.").shapes) == {"aws", "javascript", "ios"}


def test_function_words_and_one_letter_words_are_never_entities():
    assert scan("Mentored The And With For I A").shapes == ()


# ---- layer 1: no supporting fact ----------------------------------------


def test_a_rephrase_with_no_supporting_alias_is_discarded_as_no_supporting_fact():
    assert reason(edit("rephrase", "f2", "Built and ran Python services for payments", [])) == "no_supporting_fact"


def test_an_alias_that_names_no_fact_is_no_supporting_fact_whether_target_or_support():
    assert rephrase(BUILT, target="f99", supporting=("f99",)) == "no_supporting_fact"
    assert rephrase(BUILT, supporting=("f2", "f99")) == "no_supporting_fact"


def test_a_rephrase_may_only_reword_a_bullet_or_a_project_that_is_on_the_resume():
    assert rephrase("Python, again", target="f9", supporting=("f9",)) == "no_supporting_fact"  # a skill is a name
    assert rephrase("A new heading", target="f1", supporting=("f1",)) == "no_supporting_fact"  # headings are code's
    assert rephrase("Quillscript all day", target="f14", supporting=("f14",)) == "no_supporting_fact"  # not on it


# ---- layer 1: employer, title, date, number, entity ----------------------------------------


def test_naming_the_employer_of_another_role_is_changed_employer():
    assert rephrase("Contributed to the checkout migration at Globex Demo", target="f4", supporting=("f4",)) == (
        "changed_employer"
    )


def test_naming_the_employer_of_the_role_the_bullet_sits_under_is_not_a_change():
    assert rephrase("Contributed to the checkout migration at Initech Demo", target="f4", supporting=("f4",)) is None


def test_a_promoted_title_is_changed_title_and_so_is_the_title_of_another_role():
    promoted = "Contributed to the checkout migration as Senior Backend Engineer"
    other = "Contributed to the checkout migration as a Data Analyst"
    assert rephrase(promoted, target="f4", supporting=("f4",)) == "changed_title"
    assert rephrase(other, target="f4", supporting=("f4",)) == "changed_title"
    assert (
        rephrase("Contributed to the checkout migration as Backend Engineer", target="f4", supporting=("f4",)) is None
    )


FACTS_WITH_DATES = [
    role("r1", "Initech Demo", "Backend Engineer", "2019-03", "2022-08"),
    bullet("m1", "Migrated billing to the new platform in 2020", "r1"),
    bullet("m2", "Shipped the invoice portal in March 2021", "r1"),
]


@pytest.mark.parametrize(
    ("after", "target", "expected"),
    [
        ("Migrated billing to the new platform in 2019", "f2", "changed_date"),  # the fact says 2020
        ("Migrated billing to the new platform in 2020", "f2", None),
        ("Migrated billing to the new platform in March 2020", "f2", "changed_date"),  # a month the fact never gave
        ("Shipped the invoice portal in March 2021", "f3", None),
        ("Shipped the invoice portal in April 2021", "f3", "changed_date"),
        ("Shipped the invoice portal in 2021", "f3", None),  # the year alone is supported by "March 2021"
        ("Migrated billing since 2019", "f2", "changed_date"),  # the role began in 2019, but this fact does not say so
    ],
)
def test_a_date_must_be_one_the_supporting_facts_state(after, target, expected):
    assert rephrase(after, target=target, supporting=(target,), facts=FACTS_WITH_DATES) == expected


def test_a_date_is_checked_before_a_number_because_a_year_is_both():
    after = "Migrated billing in 2019 for 99 reports"  # a wrong year and a wrong number: the date is reported
    assert rephrase(after, target="f2", supporting=("f2",), facts=FACTS_WITH_DATES) == "changed_date"
    assert rephrase(
        "Migrated billing in 2020 for 99 reports", target="f2", supporting=("f2",), facts=FACTS_WITH_DATES
    ) == ("changed_number")


@pytest.mark.parametrize(
    ("after", "target", "expected"),
    [
        ("Cut report build time by 40%", "f3", "changed_number"),  # the fact says 30%
        ("Cut report build time by 30%", "f3", None),
        ("Cut report build time by thirty percent", "f3", None),  # a number word is the same number
        ("Cut report build time by forty percent", "f3", "changed_number"),
        ("Mentored two junior developers", "f5", "changed_number"),  # the fact says one
        ("Mentored one junior developer", "f5", None),
        ("Mentored 1 junior developer", "f5", None),
        ("Cut report build time", "f3", None),  # dropping a number is not inventing one
        ("Cut the build time of 12 reports by 30%", "f3", "changed_number"),
    ],
)
def test_a_number_must_be_one_the_supporting_facts_state(after, target, expected):
    assert rephrase(after, target=target, supporting=(target,)) == expected


def test_a_number_may_come_from_any_supporting_fact_not_only_the_target():
    facts = [*candidate(), bullet("t1", "Throughput of 2,000 rps", "role-initech")]
    after = "Built and ran Python services for payments at 2,000 requests a second"
    assert rephrase(after, supporting=("f2", "f15"), facts=facts) is None
    assert rephrase(after, supporting=("f2",), facts=facts) == "changed_number"  # f15 is not cited


@pytest.mark.parametrize(
    "after",
    [
        "Built Python services for payments on Kubernetes",  # a capitalised tool from the job text
        "built python services for payments on kubernetes",  # the same word in lower case: it is in the job text
        "Built Python services for payments on AWS",  # an acronym the facts do not hold
        "Built Python services for payments in C++",
        "Built Python services for payments with Node.js",
        "Built Python services for payments using Terraform",  # in the job text, not in any fact
        "Built Python services for payments for Acme",  # an organisation no fact names
    ],
)
def test_a_skill_tool_or_organisation_no_fact_holds_is_new_entity(after):
    assert rephrase(after) == "new_entity"


def test_a_confirmed_skill_is_not_a_new_entity_even_when_this_bullet_does_not_name_it():
    assert rephrase("Built Python services for payments using Quillscript") is None  # layer 2 judges the claim
    assert rephrase("Built Python services for payments using Go") is None  # Go is a skill on the resume


def test_a_word_of_the_job_text_the_facts_do_not_use_is_new_entity_but_a_word_they_do_use_is_not():
    reqs = requirements()
    scalable = [*reqs, Requirement(105, "Scalable systems", "required")]
    assert rephrase("Built scalable Python services for payments", reqs=scalable) == "new_entity"
    assert rephrase("Built Python services for payments", reqs=reqs) is None  # "Python services" is in both


def test_function_words_and_the_words_every_posting_uses_are_not_job_terms():
    reqs = [Requirement(1, "Strong experience with the team and the ability to work", "required")]
    assert rephrase("Built Python services for payments with the team", reqs=reqs) is None


def test_a_rephrase_that_only_reuses_the_words_and_numbers_of_its_supporting_facts_passes():
    assert rephrase("Built and ran Python services for payments") is None
    assert rephrase("Python services for payments, built from scratch") is None
    assert rephrase("Cut report build time by 30%", target="f3", supporting=("f3",)) is None


def test_the_reasons_are_checked_in_the_documented_order():
    """One edit with every fault gets the first reason; mend it and the next one shows."""
    after = (
        "Contributed to the checkout migration at Globex Demo as Senior Lead in 2015, saving 99 hours with Kubernetes"
    )
    faults = ["changed_employer", "changed_title", "changed_date", "changed_number", "new_entity"]
    assert rephrase(after, target="f4", supporting=("f4",)) == faults[0]
    after = after.replace(" at Globex Demo", "")
    assert rephrase(after, target="f4", supporting=("f4",)) == faults[1]
    after = after.replace(" as Senior Lead", "")
    assert rephrase(after, target="f4", supporting=("f4",)) == faults[2]
    after = after.replace(" in 2015", "")
    assert rephrase(after, target="f4", supporting=("f4",)) == faults[3]
    after = after.replace(", saving 99 hours", "")
    assert rephrase(after, target="f4", supporting=("f4",)) == faults[4]
    assert rephrase(after.replace(" with Kubernetes", ""), target="f4", supporting=("f4",)) is None
    assert "no_supporting_fact" == REASONS[0] and REASONS[1:] == tuple(faults)


# ---- layer 1: add_from_fact, reorder, remove ----------------------------------------


def test_an_add_from_fact_whose_text_is_the_fact_verbatim_passes():
    assert reason(edit("add_from_fact", None, "Quillscript", ["f14"], section="skills")) is None
    assert reason(edit("add_from_fact", None, "  Quillscript ", ["f14"], section="skills")) is None  # white space only


@pytest.mark.parametrize(
    "after",
    ["Quillscript expert", "quillscript", "Quillscript, Rust", "Scripting"],  # paraphrase, case, extra, other
)
def test_an_add_from_fact_whose_text_is_not_the_fact_verbatim_is_no_supporting_fact(after):
    assert reason(edit("add_from_fact", None, after, ["f14"], section="skills")) == "no_supporting_fact"


def test_an_add_from_fact_must_add_a_fact_the_resume_does_not_show_yet():
    assert reason(edit("add_from_fact", None, "Python", ["f9"], section="skills")) == "no_supporting_fact"
    assert reason(edit("add_from_fact", None, "Python", ["f99"], section="skills")) == "no_supporting_fact"
    assert reason(edit("add_from_fact", None, BUILT, ["f2"])) == "no_supporting_fact"


def test_an_add_from_fact_of_a_chat_stated_fact_of_a_kind_the_resume_cannot_append_is_refused():
    facts = [*candidate(), bullet("chat-b", "Led a chat-stated project", "role-initech", source="confirmed_proposal")]
    assert reason(edit("add_from_fact", None, "Led a chat-stated project", ["f15"]), facts) == "no_supporting_fact"


def test_an_exact_permutation_of_one_roles_bullets_passes():
    order = ["f3", "f2", "f5", "f4"]
    assert reason(edit("reorder", None, "", [], order)) is None
    assert reason(edit("reorder", None, "", [], ["f8", "f7"])) is None  # the other role, on its own


def test_an_exact_permutation_of_the_skills_line_passes():
    assert reason(edit("reorder", None, "", [], ["f11", "f9", "f10"], section="skills")) is None


def test_a_reorder_that_slips_an_item_in_is_new_entity():
    assert (
        reason(edit("reorder", None, "", [], ["f2", "f3", "f4", "f5", "f7"])) == "new_entity"
    )  # another role's bullet
    assert reason(edit("reorder", None, "", [], ["f9", "f10", "f11", "f14"], section="skills")) == "new_entity"
    assert reason(edit("reorder", None, "", [], ["f2", "f3", "f4", "f5", "f99"])) == "new_entity"  # no such fact


def test_a_reorder_that_drops_an_item_is_no_supporting_fact():
    assert reason(edit("reorder", None, "", [], ["f2", "f3", "f4"])) == "no_supporting_fact"
    assert reason(edit("reorder", None, "", [], ["f9", "f10"], section="skills")) == "no_supporting_fact"


def test_a_reorder_of_things_that_are_not_bullets_or_skills_is_no_supporting_fact():
    assert reason(edit("reorder", None, "", [], ["f12", "f13"])) == "no_supporting_fact"
    assert reason(edit("reorder", None, "", [], ["f99", "f98"])) == "no_supporting_fact"  # nothing to put in order


def test_a_remove_of_an_existing_bullet_or_skill_passes_and_needs_no_stated_support():
    assert reason(edit("remove", "f5", "", ["f5"])) is None
    assert reason(edit("remove", "f10", "", [], section="skills")) is None  # the fact removed supports its removal


def test_a_remove_of_something_that_is_not_a_bullet_or_skill_on_the_resume_is_no_supporting_fact():
    assert reason(edit("remove", "f99", "", [])) == "no_supporting_fact"
    assert reason(edit("remove", "f1", "", ["f1"])) == "no_supporting_fact"  # a role heading is code's, not an edit's
    assert reason(edit("remove", "f14", "", ["f14"], section="skills")) == "no_supporting_fact"  # not on the resume


def test_a_hand_added_or_corrected_bullet_is_on_the_resume_and_can_be_edited():
    facts = [*candidate(), bullet("corrected", "Fixed the payment retry loop", "role-initech", source=EDIT)]
    assert reason(edit("remove", "f15", "", ["f15"]), facts) is None
    corrected_skill = [*candidate(), skill("hand", "Terraform", source=EDIT)]
    assert reason(edit("remove", "f15", "", ["f15"], section="skills"), corrected_skill) is None


# ---- free text: the same rules for a sentence ----------------------------------------


def facts_of(*ids):
    by_id = {f.id: f for f in candidate()}
    return [by_id[i] for i in ids]


def sentence(text, supporting, *, skills=(), job_terms=(), roles=()):
    return unsupported_reason(text, facts_of(*supporting), skills=skills, job_terms=job_terms, roles=roles)


def test_free_text_applies_the_same_number_date_title_and_entity_rules():
    assert sentence("I built Python services for payments", ["b-python"]) is None
    assert sentence("I cut report build time by 40%", ["b-report"]) == "changed_number"
    assert sentence("I migrated billing in 2015", ["b-python"]) == "changed_date"
    assert sentence("I built Python services as a Senior Engineer", ["b-python"]) == "changed_title"
    assert sentence("I built Python services on Kubernetes", ["b-python"]) == "new_entity"
    assert sentence("I built Python services on kubernetes", ["b-python"], job_terms=["Kubernetes in production"]) == (
        "new_entity"
    )


def test_free_text_with_no_supporting_fact_is_no_supporting_fact():
    assert sentence("I am a great fit", []) == "no_supporting_fact"


def test_free_text_may_use_a_confirmed_skill_and_the_role_the_sentence_sits_in():
    assert sentence("I use Quillscript", ["b-python"], skills=["Quillscript"]) is None
    assert sentence("At Initech Demo I built Python services", ["role-initech", "b-python"]) is None


def test_free_text_names_another_roles_employer_or_title_when_it_is_told_the_roles():
    every_role = facts_of("role-initech", "role-globex")
    assert sentence("I built Python services at Globex Demo", ["b-python"], roles=every_role) == "changed_employer"
    assert sentence("I built Python services as a Data Analyst", ["b-python"], roles=every_role) == "changed_title"
    # told nothing about the other roles, an unfamiliar organisation is still caught, as an entity
    assert sentence("I built Python services at Globex Demo", ["b-python"]) == "new_entity"


def test_the_job_terms_may_be_whole_requirement_texts_or_single_terms():
    for terms in (["Terraform"], ["Terraform or similar infrastructure tooling"], ["infrastructure tooling"]):
        found = sentence("I built infrastructure tooling for payments", ["b-python"], job_terms=terms)
        assert found == ("new_entity" if "infrastructure tooling" in " ".join(terms) else None), terms


def test_a_sentence_that_names_no_employer_needs_no_role_fact():
    assert sentence("I built Python services for payments", ["b-python"]) is None
    assert sentence("I know SQL", ["s-sql"]) is None


# ---- all seven reasons, through both layers ----------------------------------------


def test_each_of_the_seven_reasons_is_given_to_one_seeded_edit(monkeypatch):
    """SC3. The recording (seven_reasons.json) holds one edit per reason and one clean reorder; its two answers are
    handed to the pipeline as the model's, so layer 1 and layer 2 both run on them."""

    def model_says(task, schema, messages, **kwargs):
        return schema.model_validate(SEVEN[0] if task == "tailor.propose" else SEVEN[1])

    monkeypatch.setattr(pipeline, "complete", model_says)
    facts = [*candidate(), bullet("b-extra", "Contributed to the portal launch", "role-globex")]
    proposal = pipeline.propose_and_verify(facts, requirements(), {}, principal=None, allow_rephrase=True)
    assert [(e.verifier_status, e.discard_reason) for e in proposal.edits] == [
        ("discarded", "no_supporting_fact"),
        ("discarded", "changed_employer"),
        ("discarded", "changed_title"),
        ("discarded", "changed_date"),
        ("discarded", "changed_number"),
        ("discarded", "new_entity"),
        ("discarded", "not_entailed"),
        ("passed", None),
    ]
    assert {e.discard_reason for e in proposal.edits if e.discard_reason} == set(REASONS) | {"not_entailed"}
