"""The rules ranker (AI_SYSTEM 4.5): fit from stored verdicts, the final score, who may be shown, and the day's five.

Every weight is an assumption of the design; these tests pin the arithmetic so a change to one is a visible edit.
"""

import pytest

from app.match.ranking import (
    RANKER_VERSION,
    Scored,
    fit_score,
    final_score,
    is_showable,
    pick,
    prerank_score,
    recency_bucket,
)
from app.match.validator import EvidenceRow


def row(importance, verdict):
    return EvidenceRow(1, importance, "text", verdict, () if verdict == "no_evidence" else ("fact",))


REQ_MET, REQ_PARTLY, REQ_NONE, REQ_NOT_MET = (
    row("required", "met"),
    row("required", "partly_met"),
    row("required", "no_evidence"),
    row("required", "not_met"),
)
PREF_MET, PREF_PARTLY, PREF_NONE, PREF_NOT_MET = (
    row("preferred", "met"),
    row("preferred", "partly_met"),
    row("preferred", "no_evidence"),
    row("preferred", "not_met"),
)


def test_the_version_names_the_rules_and_the_month_they_were_set():
    assert RANKER_VERSION == "rules-2026-11-a"


# ---- fit ----------------------------------------


def test_fit_is_the_weighted_verdict_score_over_the_weighted_count():
    # required weigh 2, preferred 1: (2*1 + 2*1 + 2*0.5 + 1*0) / (2 + 2 + 2 + 1) = 5/7
    assert fit_score([REQ_MET, REQ_MET, REQ_PARTLY, PREF_NONE]) == pytest.approx(5 / 7)


def test_a_required_item_that_is_not_met_costs_a_point_and_a_preferred_one_costs_nothing():
    assert fit_score([REQ_NOT_MET]) == -1.0
    assert fit_score([REQ_MET, REQ_NOT_MET]) == 0.0  # (2 - 2) / 4
    assert fit_score([REQ_MET, PREF_NOT_MET]) == pytest.approx(2 / 3)  # the preferred item only adds weight


def test_each_verdict_has_its_score():
    assert [fit_score([row("preferred", v)]) for v in ("met", "partly_met", "no_evidence", "not_met")] == [
        1.0,
        0.5,
        0.0,
        0.0,
    ]


def test_a_job_with_no_verdicts_has_fit_zero():
    assert fit_score([]) == 0.0


# ---- final and pre-rank scores ----------------------------------------


def test_final_adds_a_tenth_of_preference_match_takes_off_a_twentieth_per_seniority_step_and_adds_a_fiftieth_of_recency():
    assert final_score(0.5, 1.0, 2.0, 1.0) == pytest.approx(0.5 + 0.10 - 0.10 + 0.02)
    assert final_score(0.8, 0.0, 0.0, 0.0) == pytest.approx(0.8)
    assert final_score(0.8, -1.0, 3.0, 0.5) == pytest.approx(0.8 - 0.10 - 0.15 + 0.01)


def test_the_pre_rank_has_the_same_shape_over_the_normalized_fusion_score():
    assert prerank_score(0.8, 1.0, 2.0, 0.5) == pytest.approx(0.8 + 0.10 - 0.10 + 0.01)
    assert prerank_score(0.0, 0.0, 0.0, 0.0) == 0.0


@pytest.mark.parametrize(
    ("age_days", "bucket"),
    [(-3, 1.0), (0, 1.0), (7, 1.0), (7.5, 0.5), (30, 0.5), (30.5, 0.0), (400, 0.0)],
)
def test_recency_is_a_full_bucket_within_a_week_half_within_a_month_and_none_after(age_days, bucket):
    assert recency_bucket(age_days) == bucket


# ---- who may be shown ----------------------------------------


def test_a_job_is_showable_at_the_threshold_with_half_of_its_required_items_met_or_partly_met():
    rows = [REQ_MET, REQ_PARTLY, REQ_NONE, REQ_NONE]
    assert is_showable(0.6, rows) is True
    assert is_showable(0.59, rows) is False


def test_less_than_half_of_the_required_items_met_or_partly_met_is_not_showable_however_high_the_score():
    assert is_showable(0.95, [REQ_MET, REQ_NONE, REQ_NONE]) is False
    assert is_showable(0.95, [REQ_MET, REQ_NONE]) is True  # exactly half


def test_preferred_items_do_not_count_toward_the_half():
    assert is_showable(0.9, [REQ_NONE, PREF_MET, PREF_MET]) is False


def test_a_posting_with_no_required_items_is_judged_by_its_score_alone():
    assert is_showable(0.7, [PREF_MET]) is True
    assert is_showable(0.5, [PREF_MET]) is False


# ---- the day's five ----------------------------------------


def job(job_id, final=0.9, *, company="Acme", title="Data Engineer", rows=(REQ_MET,), weak=False):
    return Scored(job_id=job_id, company_name=company, title=title, final=final, rows=tuple(rows), weak_fit=weak)


def test_the_best_final_scores_are_shown_first_and_a_tie_goes_to_the_lower_job_id():
    scored = [job(30, 0.7, company="C"), job(20, 0.9, company="B"), job(10, 0.9, company="A"), job(40, 0.8, company="D")]
    picked = pick(scored, 4)
    assert [s.job_id for s in picked.shown] == [10, 20, 40, 30]
    assert picked.held == {}


def test_at_most_the_open_slots_are_shown_and_the_rest_are_below_the_daily_cap():
    scored = [job(1, 0.9, company="A"), job(2, 0.8, company="B"), job(3, 0.7, company="C")]
    picked = pick(scored, 2)
    assert [s.job_id for s in picked.shown] == [1, 2]
    assert picked.held == {3: "below_daily_cap"}


def test_no_open_slot_shows_nothing_and_holds_every_showable_job_back():
    picked = pick([job(1), job(2, company="B")], 0)
    assert picked.shown == ()
    assert picked.held == {1: "below_daily_cap", 2: "below_daily_cap"}


def test_one_job_per_company_and_title_is_shown_and_the_sibling_waits():
    scored = [
        job(1, 0.80, company="Acme, Inc.", title="Data Engineer (Remote)"),
        job(2, 0.85, company="ACME Inc", title="Data  Engineer - Remote"),
        job(3, 0.70, company="Acme Inc", title="Data Scientist"),
    ]
    picked = pick(scored, 5)
    assert [s.job_id for s in picked.shown] == [2, 3]
    assert picked.held == {1: "below_daily_cap"}


def test_a_sibling_that_could_not_be_shown_does_not_take_the_places_of_one_that_can():
    scored = [
        job(1, 0.95, rows=(REQ_NONE, REQ_NONE, REQ_MET)),  # not showable: one of three required items
        job(2, 0.70, rows=(REQ_MET,)),
    ]
    picked = pick(scored, 5)  # same company and title
    assert [s.job_id for s in picked.shown] == [2]
    assert picked.held == {1: "weak_fit"}


def test_every_job_that_is_not_shown_is_given_a_reason():
    scored = [
        job(1, 0.9, company="A"),
        job(2, 0.9, company="B", rows=(REQ_MET, REQ_NOT_MET, REQ_NONE)),  # a required item is not met
        job(3, 0.4, company="C", rows=(REQ_MET,)),  # below the threshold
        job(4, 0.9, company="D", weak=True),  # a met verdict on a skill with no lexical support
        job(5, 0.9, company="E", rows=(REQ_NOT_MET, REQ_MET, REQ_MET)),  # showable: half are met, score is high
    ]
    picked = pick(scored, 5)
    assert [s.job_id for s in picked.shown] == [1, 5]
    assert picked.held == {2: "required_not_met", 3: "weak_fit", 4: "weak_fit"}


def test_weak_support_is_named_before_a_not_met_item_so_the_weekly_audit_finds_it():
    picked = pick([job(1, 0.9, rows=(REQ_NOT_MET,), weak=True)], 5)
    assert picked.held == {1: "weak_fit"}


def test_the_same_input_gives_the_same_pick_whatever_its_order():
    scored = [job(n, 0.9 - n / 100, company=f"Co{n}") for n in range(1, 9)]
    first = pick(scored, 5)
    second = pick(list(reversed(scored)), 5)
    assert first == second
    assert [s.job_id for s in first.shown] == [1, 2, 3, 4, 5]
    assert first.held == {6: "below_daily_cap", 7: "below_daily_cap", 8: "below_daily_cap"}
