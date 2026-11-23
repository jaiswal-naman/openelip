"""The citation validator (AI_SYSTEM 4.4 rules 1 to 6): code that holds a model's verdicts to the request it was sent.

The requests here are small stand-ins with the attributes the validator reads; tests/match/test_evidence.py builds real
ones with build_request and validates them, so the two cannot drift apart unseen.
"""

from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from app.match.validator import Accepted, EvidenceRow, Rejected, Verdict, VerdictSet, validate

CAREER = 60  # months


def fact(fact_id, statement="Python services", *, span=(0, 15), event=False):
    return NS(id=fact_id, statement=statement, span=span, confirmed_by_event=event)


def requirement(req_id, text="Python", *, importance="required", kind="skill", min_years=None, at_span="same"):
    return NS(
        id=req_id,
        text=text,
        importance=importance,
        kind=kind,
        min_years=min_years,
        span=(0, len(text)),
        at_span=text if at_span == "same" else at_span,
    )


def request(requirements, facts=None):
    """r1..rN and f1..fM in the order given."""
    return NS(
        requirements={f"r{n}": r for n, r in enumerate(requirements, 1)},
        facts={f"f{n}": f for n, f in enumerate(facts or [], 1)},
    )


def answer(*verdicts):
    return VerdictSet(verdicts=[Verdict(requirement=r, verdict=v, facts=list(cited)) for r, v, cited in verdicts])


def check(answer_, request_, career=CAREER):
    return validate(answer_, request_, career_months=career)


ONE = request([requirement(11)], [fact("fact-uuid-1")])


# ---- a clean set ----------------------------------------


def test_a_faithful_set_is_accepted_and_each_row_maps_the_aliases_back_to_ids_in_requirement_order():
    req = request(
        [
            requirement(11, "Python", importance="required"),
            requirement(12, "Docker or Kubernetes", importance="preferred"),
            requirement(13, "Fluent French", importance="required", kind="other"),
        ],
        [fact("fact-a", "Python services"), fact("fact-b", "Docker images"), fact("fact-c", "Terraform")],
    )
    result = check(
        answer(("r3", "no_evidence", []), ("r1", "met", ["f1"]), ("r2", "partly_met", ["f2", "f3"])), req
    )  # answered out of order
    assert result == Accepted(
        rows=(
            EvidenceRow(11, "required", "Python", "met", ("fact-a",)),
            EvidenceRow(12, "preferred", "Docker or Kubernetes", "partly_met", ("fact-b", "fact-c")),
            EvidenceRow(13, "required", "Fluent French", "no_evidence", ()),
        ),
        weak_fit=False,
    )


def test_a_fact_cited_twice_in_one_verdict_is_kept_once():
    result = check(answer(("r1", "met", ["f1", "f1"])), ONE)
    assert isinstance(result, Accepted) and result.rows[0].fact_ids == ("fact-uuid-1",)


def test_the_answer_schema_forbids_extra_keys_and_unknown_verdicts():
    with pytest.raises(ValidationError):
        VerdictSet.model_validate({"verdicts": [], "note": "x"})
    with pytest.raises(ValidationError):
        VerdictSet.model_validate({"verdicts": [{"requirement": "r1", "verdict": "maybe", "facts": []}]})
    with pytest.raises(ValidationError):
        VerdictSet.model_validate({"verdicts": [{"requirement": "r1", "verdict": "met", "facts": [], "why": "x"}]})


# ---- rule 1: coverage ----------------------------------------


@pytest.mark.parametrize(
    ("verdicts", "code"),
    [
        ([], "requirement_missing"),
        ([("r1", "no_evidence", [])], "requirement_missing"),  # r2 is not answered
        ([("r1", "no_evidence", []), ("r1", "no_evidence", []), ("r2", "no_evidence", [])], "requirement_repeated"),
        ([("r1", "no_evidence", []), ("r2", "no_evidence", []), ("r9", "no_evidence", [])], "requirement_unknown"),
    ],
)
def test_a_missing_repeated_or_unknown_requirement_alias_rejects_the_set_by_rule_1(verdicts, code):
    two = request([requirement(11), requirement(12)], [fact("fact-uuid-1")])
    assert check(answer(*verdicts), two) == Rejected(1, code)


# ---- rule 2: existence ----------------------------------------


def test_a_fact_alias_that_was_not_sent_rejects_the_set_by_rule_2():
    assert check(answer(("r1", "met", ["f1", "f2"])), ONE) == Rejected(2, "fact_unknown")


def test_rules_run_in_order_so_a_set_that_breaks_rules_1_and_2_is_rejected_by_rule_1():
    assert check(answer(("r9", "met", ["f9"])), ONE) == Rejected(1, "requirement_unknown")


# ---- rule 3: spans ----------------------------------------


def test_a_requirement_whose_posting_text_at_its_span_is_not_its_stored_text_rejects_the_set_by_rule_3():
    changed = request([requirement(11, "Python", at_span="Java")], [fact("fact-uuid-1")])
    assert check(answer(("r1", "met", ["f1"])), changed) == Rejected(3, "requirement_changed")


def test_a_requirement_whose_span_could_not_be_read_rejects_the_set_by_rule_3():
    unread = request([requirement(11, at_span=None)], [fact("fact-uuid-1")])
    assert check(answer(("r1", "no_evidence", [])), unread) == Rejected(3, "requirement_changed")


def test_a_cited_fact_with_neither_a_source_span_nor_a_confirming_event_rejects_the_set_by_rule_3():
    floating = request([requirement(11)], [fact("fact-uuid-1", span=None, event=False)])
    assert check(answer(("r1", "met", ["f1"])), floating) == Rejected(3, "fact_unanchored")


def test_a_fact_confirmed_by_an_event_needs_no_source_span():
    typed_in = request([requirement(11)], [fact("fact-uuid-1", span=None, event=True)])
    assert isinstance(check(answer(("r1", "met", ["f1"])), typed_in), Accepted)


def test_a_fact_that_is_not_cited_is_not_checked_for_an_anchor():
    floating = request([requirement(11)], [fact("fact-uuid-1", span=None, event=False)])
    assert isinstance(check(answer(("r1", "no_evidence", [])), floating), Accepted)


# ---- rule 4: cardinality ----------------------------------------


@pytest.mark.parametrize("verdict", ["met", "partly_met", "not_met"])
def test_a_verdict_that_cites_nothing_rejects_the_set_by_rule_4(verdict):
    assert check(answer(("r1", verdict, [])), ONE) == Rejected(4, "cites_nothing")


def test_no_evidence_that_cites_a_fact_rejects_the_set_by_rule_4():
    assert check(answer(("r1", "no_evidence", ["f1"])), ONE) == Rejected(4, "no_evidence_cites")


# ---- rule 5: numeric sanity ----------------------------------------


def years_request(min_years, kind="experience"):
    return request([requirement(11, f"{min_years}+ years of Go", kind=kind, min_years=min_years)], [fact("fact-uuid-1")])


def test_met_on_more_years_than_the_whole_career_rejects_the_set_by_rule_5():
    assert check(answer(("r1", "met", ["f1"])), years_request(8), career=60) == Rejected(5, "years_exceed_career")


def test_met_on_exactly_the_career_length_passes():
    assert isinstance(check(answer(("r1", "met", ["f1"])), years_request(5), career=60), Accepted)


def test_only_met_is_stopped_by_the_years_check():
    for verdict in ("partly_met", "not_met"):
        assert isinstance(check(answer(("r1", verdict, ["f1"])), years_request(8), career=60), Accepted)


def test_a_requirement_with_no_stated_years_is_not_held_to_the_career_length():
    assert isinstance(check(answer(("r1", "met", ["f1"])), ONE, career=0), Accepted)


def test_with_no_dated_career_a_met_on_a_years_requirement_is_rejected():
    assert check(answer(("r1", "met", ["f1"])), years_request(1), career=0) == Rejected(5, "years_exceed_career")


# ---- rule 6: lexical support ----------------------------------------


def skill_request(text, statement, kind="skill"):
    return request([requirement(11, text, kind=kind)], [fact("fact-uuid-1", statement)])


def test_met_on_a_skill_whose_cited_facts_share_no_term_with_it_is_accepted_but_marked_weak():
    result = check(answer(("r1", "met", ["f1"])), skill_request("Kubernetes", "Terraform"))
    assert isinstance(result, Accepted) and result.weak_fit is True
    assert result.rows[0].verdict == "met"  # the verdict is kept as given; the caller decides what to do with the flag


def test_met_on_a_skill_that_shares_a_term_with_a_cited_fact_is_not_weak():
    result = check(answer(("r1", "met", ["f1"])), skill_request("Strong Kubernetes experience", "Ran Kubernetes clusters"))
    assert isinstance(result, Accepted) and result.weak_fit is False


def test_filler_words_are_not_support():
    result = check(
        answer(("r1", "met", ["f1"])),
        skill_request("Strong experience with the Rust language", "Strong experience with the team"),
    )
    assert isinstance(result, Accepted) and result.weak_fit is True


def test_plural_and_singular_forms_count_as_the_same_term():
    result = check(answer(("r1", "met", ["f1"])), skill_request("Experience writing tests", "Wrote a test harness"))
    assert isinstance(result, Accepted) and result.weak_fit is False


def test_terms_with_symbols_are_kept_whole_so_c_is_not_c_plus_plus():
    assert check(answer(("r1", "met", ["f1"])), skill_request("C++", "C#")).weak_fit is True
    assert check(answer(("r1", "met", ["f1"])), skill_request("C++", "C++ and Rust")).weak_fit is False


def test_the_support_check_applies_to_met_on_skill_requirements_only():
    for verdict, kind in (("partly_met", "skill"), ("met", "experience"), ("met", "education")):
        result = check(answer(("r1", verdict, ["f1"])), skill_request("Kubernetes", "Terraform", kind=kind))
        assert isinstance(result, Accepted) and result.weak_fit is False, (verdict, kind)


def test_the_support_is_judged_over_all_cited_facts_together():
    req = request([requirement(11, "Kubernetes")], [fact("fact-a", "Terraform"), fact("fact-b", "Kubernetes operator")])
    assert check(answer(("r1", "met", ["f1", "f2"])), req).weak_fit is False


def test_rule_5_is_checked_before_rule_6():
    req = request(
        [requirement(11, "8+ years of Kubernetes", kind="skill", min_years=8)], [fact("fact-uuid-1", "Terraform")]
    )
    assert check(answer(("r1", "met", ["f1"])), req, career=60) == Rejected(5, "years_exceed_career")
