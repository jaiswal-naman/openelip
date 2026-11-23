"""CHT-01: the decision reads the chat tools call, as the public functions of app.match.api (plan 05-03).

get_decision, list_decisions and decision_for_job wrap the ledger reads of plan 04-04 and hand back frozen views with
real ids (the chat layer turns them into aliases). Rows are invented and built with the 04-01 factories on the
rolled-back `conn` fixture; a second person has rows of her own throughout, on the same jobs.
"""

from dataclasses import FrozenInstanceError
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.match import api
from app.profile import service
from tests.match.factories import (
    LOCAL_DATE,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

D0 = LOCAL_DATE
D1, D2, D3 = (LOCAL_DATE - timedelta(days=n) for n in (1, 2, 3))
SALARY = {"salary_floor": {"amount": 3_000_000, "currency": "INR"}}


def add_evidence(conn, decision: UUID, user: UUID, job, verdicts: list[Verdict]) -> None:
    """The verdicts of a job that was scored and then held back (a shown one gets them from make_shown_decision)."""
    for requirement, verdict in zip(job.requirement_ids, verdicts, strict=True):
        conn.execute(
            text(
                """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids)
                   VALUES (:d, :u, :r, CAST(:verdict AS evidence_verdict), CAST(:facts AS uuid[]))"""
            ),
            {"d": decision, "u": user, "r": requirement, "verdict": verdict.verdict, "facts": list(verdict.fact_ids)},
        )


@pytest.fixture
def world(conn) -> SimpleNamespace:
    """Ada with a salary floor and an excluded company; four days of decisions (a shown match, a scored job held back
    for its gaps, a job her salary floor removed, an unscored one); Bob with one on Ada's held-back job."""
    w = SimpleNamespace()
    w.ada, w.bob = make_user(conn), make_user(conn)
    w.record = make_record(conn, w.ada)
    make_record(conn, w.bob)
    w.floor = service.set_constraint(conn, w.ada, "salary_floor", SALARY, "Nothing below 30 lakh a year.").row["id"]
    w.never = service.set_constraint(
        conn, w.ada, "excluded_company", {"company_names": ["Ashgrove Capital"]}, "Never Ashgrove Capital."
    ).row["id"]
    w.good = make_job(
        conn,
        title="Data engineer",
        description="Build pipelines in Python.",
        requirements=[("Python", "required")],
        country="IN",
    )
    w.managing = make_job(
        conn,
        title="Engineering manager",
        description="Manage teams. Three years managing engineering teams. Python is a plus.",
        requirements=[("Three years managing engineering teams", "required"), ("Python", "preferred")],
        company_name="Corvid Pay",
    )
    w.cheap = make_job(
        conn,
        title="Backend engineer II",
        description="Backend work.",
        requirements=[("Backend work", "required")],
        company_name="Ashgrove Capital",
        salary_min=1_800_000,
        salary_max=2_400_000,
        salary_currency="INR",
    )
    w.unscored = make_job(
        conn, title="Analyst", description="Analyse data.", requirements=[("Analyse data", "required")]
    )
    runs = {day: make_run(conn, w.ada, local_date=day) for day in (D0, D1, D2, D3)}
    w.shown = make_shown_decision(
        conn,
        runs[D0],
        w.ada,
        w.good,
        [Verdict("met", w.record.fact_ids[1:2], note="Python pipelines")],
        rank_position=1,
        summary="A fit, with the evidence below.",
        unverified=("salary_not_stated",),
    )
    w.gapped = make_decision(
        conn, runs[D1], w.ada, w.managing, outcome="withheld", reasons=("required_not_met",), retrieval_rank=2
    )
    add_evidence(
        conn, w.gapped, w.ada, w.managing, [Verdict("no_evidence"), Verdict("partly_met", w.record.fact_ids[1:2])]
    )
    w.filtered = make_decision(
        conn,
        runs[D2],
        w.ada,
        w.cheap,
        outcome="filtered",
        reasons=("hard_constraint",),
        violated_kinds=("salary_floor", "excluded_company"),
    )
    w.old = make_decision(
        conn, runs[D3], w.ada, w.unscored, outcome="withheld", reasons=("not_scored",), retrieval_rank=1
    )
    w.bobs = make_decision(
        conn, make_run(conn, w.bob, local_date=D0), w.bob, w.managing, outcome="withheld", reasons=("not_scored",)
    )
    return w


# ---- get_decision --------------------------------------------------------------------------------------------------


def test_a_shown_decision_comes_back_with_its_job_reasons_and_requirement_by_requirement_evidence(conn, world):
    view = api.get_decision(conn, world.ada, world.shown)

    assert (view.decision_id, view.job_id, view.job_title, view.company_name) == (
        world.shown,
        world.good.id,
        "Data engineer",
        "Initech Demo",
    )
    assert (view.outcome, view.reason_codes, view.local_date) == ("shown", ("top_ranked",), D0)
    assert (view.violated_kinds, view.constraints, view.unverified) == ((), (), ("salary_not_stated",))
    assert (
        view.summary == "A fit, with the evidence below." and view.change_note is None and view.is_on_request is False
    )
    assert view.evidence == (
        api.DecisionEvidence(
            requirement_id=world.good.requirement_ids[0],
            requirement="Python",
            importance="required",
            verdict="met",
            fact_ids=(world.record.fact_ids[1],),
        ),
    )


def test_a_filtered_decision_names_every_kind_it_broke_with_the_constraint_ids_the_ledger_has(conn, world):
    view = api.get_decision(conn, world.ada, world.filtered)

    assert (view.outcome, view.reason_codes) == ("filtered", ("hard_constraint",))
    assert view.violated_kinds == ("salary_floor", "excluded_company")
    assert view.constraints == (
        api.DecisionConstraint("salary_floor", UUID(world.floor), "Nothing below 30 lakh a year."),
        api.DecisionConstraint("excluded_company", UUID(world.never), "Never Ashgrove Capital."),
    )
    assert view.evidence == ()  # a job that was filtered was never scored


def test_a_constraint_the_person_removed_since_leaves_its_kind_and_no_id_or_sentence(conn, world):
    service.remove_constraint(conn, world.ada, UUID(world.floor))
    view = api.get_decision(conn, world.ada, world.filtered)
    assert view.constraints == (
        api.DecisionConstraint("salary_floor", None, None),  # never guessed
        api.DecisionConstraint("excluded_company", UUID(world.never), "Never Ashgrove Capital."),
    )


def test_a_scored_job_held_back_carries_its_evidence_in_requirement_order(conn, world):
    view = api.get_decision(conn, world.ada, world.gapped)
    assert [(e.requirement, e.importance, e.verdict, e.fact_ids) for e in view.evidence] == [
        ("Three years managing engineering teams", "required", "no_evidence", ()),
        ("Python", "preferred", "partly_met", (world.record.fact_ids[1],)),
    ]


def test_another_persons_decision_and_an_unknown_id_are_none(conn, world):
    assert api.get_decision(conn, world.ada, world.bobs) is None
    assert api.get_decision(conn, world.bob, world.shown) is None
    assert api.get_decision(conn, world.ada, uuid4()) is None


def test_a_view_cannot_be_changed(conn, world):
    view = api.get_decision(conn, world.ada, world.shown)
    with pytest.raises(FrozenInstanceError):
        view.outcome = "withheld"  # type: ignore[misc]


# ---- list_decisions ------------------------------------------------------------------------------------------------


def test_the_list_is_the_persons_own_decisions_newest_date_first_without_evidence(conn, world):
    rows = api.list_decisions(conn, world.ada)
    assert [r.decision_id for r in rows] == [world.shown, world.gapped, world.filtered, world.old]  # Bob's is not here
    assert all(r.evidence == () for r in rows)  # a list is for choosing; get_decision has the table
    assert [r.job_title for r in rows] == ["Data engineer", "Engineering manager", "Backend engineer II", "Analyst"]
    filtered = rows[2]
    assert filtered.violated_kinds == ("salary_floor", "excluded_company")
    assert [c.constraint_id for c in filtered.constraints] == [UUID(world.floor), UUID(world.never)]


def test_the_list_filters_by_outcome_and_by_date_range(conn, world):
    def of(**filters):
        return [r.decision_id for r in api.list_decisions(conn, world.ada, **filters)]

    assert of(outcome="filtered") == [world.filtered]
    assert of(outcome="withheld") == [world.gapped, world.old]
    assert of(outcome="shown") == [world.shown]
    assert of(since=D1) == [world.shown, world.gapped]
    assert of(until=D1) == [world.gapped, world.filtered, world.old]
    assert of(since=D2, until=D1) == [world.gapped, world.filtered]
    assert of(outcome="withheld", since=D1) == [world.gapped]
    assert of(outcome="filtered", since=D0) == []


def test_the_list_stops_at_the_limit(conn, world):
    assert [r.decision_id for r in api.list_decisions(conn, world.ada, limit=2)] == [world.shown, world.gapped]
    assert len(api.list_decisions(conn, world.ada, limit=20)) == 4


def test_the_list_refuses_an_outcome_that_is_not_one(conn, world):
    with pytest.raises(ValueError):
        api.list_decisions(conn, world.ada, outcome="approved")


def test_bobs_list_holds_only_his_decision(conn, world):
    assert [r.decision_id for r in api.list_decisions(conn, world.bob)] == [world.bobs]


# ---- decision_for_job ----------------------------------------------------------------------------------------------


def test_a_job_with_a_stored_decision_returns_the_newest_one_with_its_evidence(conn, world):
    older = make_run(conn, world.ada, local_date=D3, trigger="on_demand")
    make_decision(conn, older, world.ada, world.managing, outcome="withheld", reasons=("not_scored",))
    view = api.decision_for_job(conn, world.ada, world.managing.id)
    assert view.decision_id == world.gapped and view.is_on_request is False  # D1 is newer than D3
    assert [e.verdict for e in view.evidence] == ["no_evidence", "partly_met"]


def test_a_job_with_no_stored_decision_is_checked_against_the_constraints_on_the_spot(conn, world):
    broken = make_job(
        conn,
        title="Backend engineer III",
        description="Backend work.",
        requirements=[],
        company_name="Ashgrove Capital",
        country="IN",
    )
    clear = make_job(conn, title="Data scientist", description="Models.", requirements=[], country="IN")

    stopped = api.decision_for_job(conn, world.ada, broken.id)
    assert stopped.decision_id is None and stopped.is_on_request is True
    assert (stopped.outcome, stopped.reason_codes, stopped.violated_kinds) == (
        "filtered",
        ("hard_constraint",),
        ("excluded_company",),
    )
    assert stopped.constraints == (
        api.DecisionConstraint("excluded_company", UUID(world.never), "Never Ashgrove Capital."),
    )
    assert (stopped.job_id, stopped.job_title, stopped.company_name) == (
        broken.id,
        "Backend engineer III",
        "Ashgrove Capital",
    )

    passed = api.decision_for_job(conn, world.ada, clear.id)
    assert (passed.decision_id, passed.outcome, passed.violated_kinds, passed.evidence) == (
        None,
        "not_evaluated",
        (),
        (),
    )


def test_another_persons_decision_on_the_same_job_is_not_returned_for_me(conn, world):
    """A job only Bob has a decision on gets the on-request check for Ada, never Bob's row."""
    only_bobs = make_job(conn, title="Support engineer", description="Help.", requirements=[], country="IN")
    make_decision(conn, make_run(conn, world.bob, local_date=D1), world.bob, only_bobs, reasons=("not_scored",))

    view = api.decision_for_job(conn, world.ada, only_bobs.id)
    assert view.decision_id is None and view.is_on_request is True
    assert api.decision_for_job(conn, world.bob, only_bobs.id).decision_id is not None


def test_an_unknown_job_is_none(conn, world):
    assert api.decision_for_job(conn, world.ada, 987_654_321) is None
