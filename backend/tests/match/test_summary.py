"""The one-line summary of a match (AI_SYSTEM 4.6): rendered by code from the evidence rows, no model."""

from app.match.checker import JobFacts
from app.match.summary import render
from app.match.validator import EvidenceRow


def row(verdict, text="Python", importance="required"):
    return EvidenceRow(1, importance, text, verdict, () if verdict == "no_evidence" else ("fact",))


def job(**fields):
    return JobFacts(company_name="Initech Demo", **fields)


def test_the_example_of_the_design_document():
    rows = [row("met")] * 6 + [row("no_evidence", "Kubernetes"), row("no_evidence", "Terraform")]
    assert (
        render(rows, job(country="IN", work_arrangements=("remote",)))
        == "Meets 6 of 8 required items. No evidence for Kubernetes. Remote, India."
    )


def test_a_job_with_no_stated_place_says_so():
    assert render([row("met")], job()) == "Meets 1 of 1 required item. Location not stated."


def test_a_city_and_a_country_follow_the_arrangement():
    assert render([row("met")], job(country="IN", city="Pune", work_arrangements=("onsite",))).endswith(
        " On-site, Pune, India."
    )


def test_several_arrangements_are_named_in_a_fixed_order():
    posting = job(country="DE", work_arrangements=("remote", "onsite", "hybrid"))
    assert render([row("met")], posting).endswith(" On-site, hybrid or remote, Germany.")


def test_a_place_without_an_arrangement_and_an_arrangement_without_a_place():
    assert render([row("met")], job(country="US")).endswith(" United States.")
    assert render([row("met")], job(work_arrangements=("hybrid",))).endswith(" Hybrid. Location not stated.")


def test_a_country_code_the_gazetteer_does_not_know_is_shown_as_it_came():
    assert render([row("met")], job(country="ZZ")).endswith(" ZZ.")


def test_only_required_items_are_counted_and_only_a_met_verdict_counts_as_met():
    rows = [row("met"), row("partly_met"), row("met", importance="preferred"), row("not_met", importance="preferred")]
    assert render(rows, job()).startswith("Meets 1 of 2 required items.")


def test_a_required_item_that_is_not_met_is_named_before_one_with_no_evidence():
    rows = [row("no_evidence", "Go"), row("not_met", "8+ years of Java"), row("not_met", "Rust")]
    text = render(rows, job())
    assert "Not met: 8+ years of Java. No evidence for Go." in text


def test_only_the_first_of_each_is_named_and_a_preferred_gap_is_not_named():
    rows = [row("no_evidence", "Go", importance="preferred"), row("no_evidence", "Rust"), row("no_evidence", "Java")]
    text = render(rows, job())
    assert "No evidence for Rust." in text and "Java" not in text and "Go" not in text


def test_a_posting_with_no_required_items_says_so():
    assert render([row("met", importance="preferred")], job(country="IN")).startswith("No required items listed. ")


def test_a_long_requirement_is_cut_with_an_ellipsis_and_a_closing_full_stop_is_not_doubled():
    words = "Experience with Kubernetes in production, including Helm and operators and more of it."
    named = render([row("no_evidence", words)], job()).split("No evidence for ")[1].split(" Location")[0]
    assert named.endswith("….") and len(named) <= 81
    assert render([row("no_evidence", "Kubernetes.")], job()).count("Kubernetes.") == 1  # one full stop, not two


def test_line_breaks_in_a_requirement_become_spaces():
    assert "No evidence for Go and Rust." in render([row("no_evidence", "Go\nand\tRust")], job())
