"""Plan 07-03, key two: the six checks, each reason code, and the fail-closed citation check. The pure checks run on a
CheckInput built by hand; the end to end reads of gather() are covered by test_propose and the invariant tests."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.guardian import checks
from app.guardian.checks import CheckInput, CitedSentence
from app.guardian.policy import SubjectGate

GATE = SubjectGate("campaign", uuid4(), uuid4(), True, None, False, 1, None, "talent@lumen.example", 750_000)
APPLIED = "I applied on 3 November 2026."
FACT = {
    "id": "fact-1",
    "kind": "achievement",
    "statement": "Cut report time from 6 hours to 40 minutes.",
    "attributes": {},
}
CLAIM = {
    "text": "Lumen Demo builds lighting software.",
    "sources": [{"url": "https://lumen.example/about", "quote": "We build lighting software.", "retrieved_at": "t"}],
}


def follow_up(**changes) -> CheckInput:
    base = CheckInput(
        action_type="send_follow_up",
        gate=GATE,
        texts={"subject": "Following up", "body": f"Hello,\n\n{APPLIED}\n\nBest regards,\nAda"},
        sources={"subject": "code", "body": "model"},
        recipient="talent@lumen.example",
        sentences=(CitedSentence("body", APPLIED, events=(APPLIED,)),),
        code_parts=("Hello,", "Best regards,\nAda"),
    )
    return replace(base, **changes)


@pytest.fixture(autouse=True)
def passing_citations():
    before = checks._citation_check
    checks.register_citation_check(lambda sentences, **k: [None] * len(sentences))
    yield
    checks.register_citation_check(before)


def result(inp: CheckInput, kind: str):
    return next(r for r in checks.run(inp, principal=None) if r.kind == kind)


def test_a_clean_follow_up_passes_all_six_checks_in_order():
    results = checks.run(follow_up(), principal=None)
    assert [r.kind for r in results] == list(checks.CHECK_KINDS) and all(r.passed for r in results)
    assert checks.failed(results) == [] and checks.GUARDIAN_VERSION.startswith("guardian-")


# ---- citations ---------------------------------------------------------------------------------------------------


def test_with_no_citation_check_registered_citations_fails_closed():
    checks.register_citation_check(None)
    found = result(follow_up(), "citations")
    assert (found.passed, found.reason_code) == (False, "citation_check_missing")


def test_a_cited_fact_that_is_not_usable_is_unresolved():
    inp = follow_up(sentences=(CitedSentence("body", APPLIED, fact_ids=("fact-9",)),))
    assert result(inp, "citations").reason_code == "citation_unresolved"


def test_a_cited_claim_out_of_range_or_without_a_source_is_unresolved():
    sentence = CitedSentence("body", APPLIED, claim_indexes=(3,))
    assert result(follow_up(sentences=(sentence,), claims=(CLAIM,)), "citations").reason_code == "citation_unresolved"
    bare = {"text": "x", "sources": []}
    sentence = CitedSentence("body", APPLIED, claim_indexes=(0,))
    assert result(follow_up(sentences=(sentence,), claims=(bare,)), "citations").reason_code == "citation_unresolved"


def test_text_the_provenance_does_not_account_for_is_unresolved():
    inp = follow_up(
        texts={
            "subject": "s",
            "body": f"Hello,\n\n{APPLIED}\n\nI also led the Kubernetes migration.\n\nBest regards,\nAda",
        }
    )
    found = result(inp, "citations")
    assert (found.reason_code, found.details["why"]) == ("citation_unresolved", "text_not_accounted_for")


def test_a_listed_sentence_the_field_does_not_hold_is_unresolved():
    inp = follow_up(sentences=(CitedSentence("body", "A sentence that is not in the body.", events=("x",)),))
    assert result(inp, "citations").reason_code == "citation_unresolved"


@pytest.mark.parametrize("reason", ["unsupported_claim", "citation_unresolved", "changed_number"])
def test_the_registered_checks_verdict_becomes_one_of_the_two_codes(reason):
    checks.register_citation_check(lambda sentences, **k: [reason] * len(sentences))
    found = result(follow_up(), "citations")
    assert found.reason_code == (
        reason if reason in ("unsupported_claim", "citation_unresolved") else "unsupported_claim"
    )
    assert found.details["quote"] == APPLIED


def test_the_registered_check_gets_the_resolved_sentences_facts_claims_and_the_principal():
    seen = {}

    def spy(sentences, *, facts, claims, skills, job_terms, principal):
        seen.update(
            sentences=list(sentences),
            facts=dict(facts),
            claims=list(claims),
            skills=skills,
            job_terms=job_terms,
            principal=principal,
        )
        return [None] * len(sentences)

    checks.register_citation_check(spy)
    who = uuid4()
    sentence = CitedSentence("body", APPLIED, fact_ids=("fact-1",), claim_indexes=(0,), events=(APPLIED,))
    inp = follow_up(
        sentences=(sentence,), facts={"fact-1": FACT}, claims=(CLAIM,), skills=("Python",), job_terms=("ETL",)
    )
    assert checks.run(inp, principal=who)[0].passed
    assert seen["facts"] == {"fact-1": FACT} and seen["claims"] == [CLAIM] and seen["principal"] == who
    assert seen["skills"] == ("Python",) and seen["job_terms"] == ("ETL",)


def test_candidate_written_fields_skip_citations_and_injection():
    inp = follow_up(
        texts={"subject": "Following up", "body": "Hi, see https://evil.example/x for my work."},
        sources={"subject": "code", "body": "candidate"},
        sentences=(),
        input_flags=("page_flagged",),
    )
    checks.register_citation_check(None)  # not even needed
    assert result(inp, "citations").details == {"applies": False}
    assert result(inp, "injection").details == {"applies": False} and result(inp, "injection").passed


# ---- consent -----------------------------------------------------------------------------------------------------


def test_consent_fails_without_ai_processing():
    found = result(follow_up(consent=False), "consent")
    assert (found.passed, found.reason_code) == (False, "consent_missing")


# ---- scope -------------------------------------------------------------------------------------------------------


def test_scope_fails_when_the_job_now_breaks_a_hard_constraint():
    found = result(follow_up(violations=("employment_type", "seniority")), "scope")
    assert (found.reason_code, found.details["kinds"]) == ("constraint_conflict", ["employment_type", "seniority"])


def test_scope_fails_for_a_blocked_employer():
    assert result(follow_up(violations=("excluded_company",)), "scope").reason_code == "employer_blocked"


@pytest.mark.parametrize("recipient", ["employer@lumen.example", None, ""])
def test_scope_refuses_a_follow_up_to_any_address_but_the_typed_one(recipient):
    assert result(follow_up(recipient=recipient), "scope").reason_code == "restricted_data"


def test_scope_refuses_a_follow_up_when_the_candidate_typed_no_address():
    inp = follow_up(gate=replace(GATE, follow_up_email=None))
    assert result(inp, "scope").reason_code == "restricted_data"


def test_the_typed_address_matches_without_regard_to_case_and_spaces():
    assert result(follow_up(recipient=" Talent@Lumen.Example "), "scope").passed


@pytest.mark.parametrize(
    ("text", "what"),
    [
        ("Reach me on ada@mail.example please.", "contact_details"),
        ("Call me on +44 20 7946 0958.", "contact_details"),
        ("My salary expectation is flexible.", "pay"),
        ("I earn $95,000 today.", "pay"),
        ("My notice period is two months.", "pay"),
        ("I am a woman of faith and my religion matters.", "protected_characteristic"),
        ("I am pregnant.", "protected_characteristic"),
    ],
)
def test_scope_refuses_restricted_data_in_any_field_including_the_candidates_own(text, what):
    inp = follow_up(texts={"subject": "s", "body": text}, sources={"subject": "code", "body": "candidate"})
    found = result(inp, "scope")
    assert (found.reason_code, what in found.details["what"]) == ("restricted_data", True)


def test_scope_refuses_the_salary_floor_in_a_message():
    inp = follow_up(texts={"subject": "s", "body": "I would need 120000 to move."}, salary_floor=120_000)
    assert "salary_floor" in result(inp, "scope").details["what"]
    inp = follow_up(texts={"subject": "s", "body": "I would need 120k to move."}, salary_floor=120_000)
    assert "salary_floor" in result(inp, "scope").details["what"]


def test_a_date_is_not_mistaken_for_a_phone_number():
    assert result(follow_up(), "scope").passed


# ---- budget ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["campaign_exhausted", "month_exhausted"])
def test_budget_fails_when_either_limit_is_used_up(which):
    found = result(follow_up(**{which: True}), "budget")
    assert (found.reason_code, found.details[which.split("_")[0]]) == ("budget_exhausted", True)


# ---- injection ---------------------------------------------------------------------------------------------------


def test_an_input_flag_fails_injection():
    found = result(follow_up(input_flags=("page_flagged",)), "injection")
    assert (found.reason_code, found.details["flags"]) == ("injection_suspected", ["page_flagged"])


def test_a_cited_claim_flagged_as_an_injection_fails_injection():
    flagged = {**CLAIM, "injection_suspected": True}
    sentence = CitedSentence("body", APPLIED, claim_indexes=(0,), events=(APPLIED,))
    assert result(follow_up(sentences=(sentence,), claims=(flagged,)), "injection").reason_code == "injection_suspected"


@pytest.mark.parametrize(
    "text", ["See https://elsewhere.example/me.", "Write to boss@elsewhere.example.", "Ring 020 7946 0958 now."]
)
def test_a_url_email_or_phone_that_is_not_from_the_record_or_the_employer_fails_injection(text):
    inp = follow_up(
        texts={"subject": "s", "body": f"Hello,\n\n{text}\n\nBest regards,\nAda"},
        sentences=(CitedSentence("body", text, events=("x",)),),
    )
    assert result(inp, "injection").reason_code == "injection_suspected"


def test_an_address_the_record_or_the_employers_own_page_holds_passes_injection():
    text = "My portfolio is at https://ada.example/work."
    fact = {**FACT, "statement": "Portfolio: https://ada.example/work"}
    inp = follow_up(
        texts={"subject": "s", "body": f"Hello,\n\n{text}\n\nBest regards,\nAda"},
        sentences=(CitedSentence("body", text, fact_ids=("fact-1",)),),
        facts={"fact-1": fact},
    )
    assert result(inp, "injection").passed
    text = "The posting is at https://careers.lumen.example/apply."
    inp = follow_up(
        texts={"subject": "s", "body": f"Hello,\n\n{text}\n\nBest regards,\nAda"},
        sentences=(CitedSentence("body", text, events=("x",)),),
        employer_urls=("https://careers.lumen.example/apply",),
    )
    assert result(inp, "injection").passed


# ---- rate limit --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("sent", "ok"), [(0, True), (1, True), (2, False), (3, False)])
def test_a_third_follow_up_hits_the_limit(sent, ok):
    found = result(follow_up(follow_ups=sent), "rate_limit")
    assert (found.passed, found.reason_code) == (ok, None if ok else "limit_reached")


def test_a_packet_has_no_rate_limit():
    inp = follow_up(action_type="submit_application", follow_ups=5)
    assert result(inp, "rate_limit").details == {"applies": False}


def test_text_fields_reads_the_words_of_each_payload_type():
    assert checks.text_fields("send_follow_up", {"subject": "S", "body": "B", "to": "x@y.example"}) == {
        "subject": "S",
        "body": "B",
    }
    payload = {
        "cover_note": "Note",
        "answers": [{"question_id": "why", "answer": "Because"}, {"question_id": "pay", "answer": ""}],
    }
    assert checks.text_fields("submit_application", payload) == {"cover_note": "Note", "answers.why": "Because"}
