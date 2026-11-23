"""HIR-04 and HIR-06 (plan 06-05, task 2): the pool, the ask job and the ask itself.

Everything is committed (the role run opens its own connections) and invented (tests/hiring/factories.py). The
model is the real gateway on the fake provider answering from tests/match/fixtures/role_run.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.hiring import ask_jobs, asks
from app.match.api import run_role
from app.trust import api as trust
from tests.hiring import asks_world
from tests.hiring import factories as hiring
from tests.hiring import world as hiring_world
from tests.hiring.asks_world import introductions, make_candidate, make_role, role_runs, worker
from tests.match.fixtures.run import world


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    yield from asks_world.committed(engine, monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    return asks_world.provider(monkeypatch)


def pool_of(engine, role):
    open_role = worker(asks.open_role, role.id)
    return worker(asks.pool, open_role)


# --- the pool: each exclusion has its own test -----------------------------------------------------------------------


def test_an_open_unasked_candidate_is_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)

    assert pool_of(engine, role) == [ada.id]


def test_a_candidate_not_open_to_introductions_is_not_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    make_candidate(engine, open_to_introductions=False)

    assert pool_of(engine, role) == []


def test_a_candidate_who_withdrew_open_to_introductions_is_not_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        assert trust.withdraw_consent(c, ada.id, "open_to_introductions") == 1

    assert pool_of(engine, role) == []


def test_a_candidate_without_ai_processing_consent_is_not_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        trust.withdraw_consent(c, ada.id, "ai_processing")

    assert pool_of(engine, role) == []


def test_a_suspended_account_and_an_unconfirmed_record_are_not_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    gone, unconfirmed, kept = make_candidate(engine), make_candidate(engine), make_candidate(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": gone.id})
        c.execute(text("UPDATE users SET record_confirmed_at = NULL WHERE id = :u"), {"u": unconfirmed.id})

    assert pool_of(engine, role) == [kept.id]


def test_a_synthetic_candidate_is_not_asked_by_a_real_organization_but_is_by_a_synthetic_one(engine, provider):
    provider()
    with engine.begin() as c:
        real = hiring.make_org(c, "Real Hiring Co", is_synthetic=False)
    real_role = make_role(engine, org=real)
    demo_role = make_role(engine)
    make_candidate(engine)  # synthetic by default
    real_person = make_candidate(engine, email="real.person@example.com", is_synthetic=False)

    assert pool_of(engine, real_role) == [real_person.id]
    assert len(pool_of(engine, demo_role)) == 2


def test_a_candidate_already_asked_about_this_role_is_not_in_the_pool(engine, provider):
    provider()
    role = make_role(engine)
    asked = make_candidate(engine)
    with engine.begin() as c:
        hiring.make_ask(c, role, asked)

    assert pool_of(engine, role) == []


def test_a_member_of_the_organization_is_not_in_the_pool(engine, provider):
    provider()
    with engine.begin() as c:
        org = hiring.make_org(c)
        member = hiring.make_candidate(c)
        c.execute(
            text(
                "INSERT INTO org_members (organization_id, user_id, role, status, joined_at) "
                "VALUES (:o, :u, 'member', 'active', now())"
            ),
            {"o": org.id, "u": member.id},
        )
    role = make_role(engine, org=org)
    other = make_candidate(engine)

    assert pool_of(engine, role) == [other.id]


# --- the ask job ---


def run_job(role):
    worker(ask_jobs.ask_candidates, {"role_id": str(role.id)})


def test_the_job_asks_each_selected_candidate_once_with_the_frozen_preview_and_seven_days(engine, provider):
    provider()
    role = make_role(engine)
    ada, bob = make_candidate(engine, name="Demo Ada"), make_candidate(engine, name="Demo Bob")

    run_job(role)

    asked = introductions(engine, role)
    assert sorted(i.user_id for i in asked) == sorted([ada.id, bob.id])
    for i in asked:
        assert i.status == "asked" and i.expires_at == i.asked_at + timedelta(days=7)
        assert set(i.share_preview) == {"on_accept", "on_employer_yes"}
        assert i.share_preview["on_accept"]["name"] in ("Demo Ada", "Demo Bob")
        assert len(i.share_preview["on_accept"]["evidence"]) == 3
    (run,) = role_runs(engine, role)
    assert run.status == "completed"


def test_nobody_withheld_or_filtered_is_asked(engine, provider):
    provider("required_not_met.json")
    role = make_role(engine, country="IN", arrangements=("remote",))
    make_candidate(engine)
    blocked = make_candidate(engine)
    with engine.begin() as c:
        hiring_world.exclude_company(c, blocked.id, role.company)

    run_job(role)

    assert introductions(engine, role) == []
    (run,) = role_runs(engine, role)
    assert run.status == "completed"


def test_with_no_provider_nobody_is_asked_and_the_run_is_recorded(engine, provider):
    provider(LLM_PROVIDER="anthropic")  # no key
    role = make_role(engine)
    make_candidate(engine)

    run_job(role)

    assert introductions(engine, role) == []
    (run,) = role_runs(engine, role)
    assert (run.status, run.evidence_prompt_version) == ("completed", None)


def test_a_closed_role_is_not_run(engine, provider):
    provider()
    role = make_role(engine)
    make_candidate(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE roles SET status = 'closed', closed_at = now() WHERE id = :r"), {"r": role.id})

    run_job(role)

    assert introductions(engine, role) == [] and role_runs(engine, role) == []


def test_an_organization_with_an_overdue_answer_creates_no_run_and_no_ask_then_resumes_when_answered(engine, provider):
    provider()
    with engine.begin() as c:
        org = hiring.make_org(c)
    first_role = make_role(engine, org=org)
    role = make_role(engine, org=org)
    waiting = make_candidate(engine)
    other = make_candidate(engine)
    with engine.begin() as c:
        accepted = hiring.make_accepted(c, first_role, waiting)
        hiring_world.overdue(c, accepted.id)

    run_job(role)

    assert role_runs(engine, role) == [] and introductions(engine, role) == []

    with engine.begin() as c:  # the employer answers: the clock stops
        c.execute(
            text(
                """UPDATE introductions SET employer_answer = 'no', employer_answer_reason = 'Not now.',
                          employer_answered_by = :u, employer_answered_at = now() WHERE id = :i"""
            ),
            {"u": org.owner, "i": accepted.id},
        )
    run_job(role)

    assert sorted(i.user_id for i in introductions(engine, role)) == sorted([waiting.id, other.id])  # asking resumed


def test_a_refusal_by_the_guard_skips_that_candidate_only(engine, provider):
    provider()
    role = make_role(engine)
    keep, lose = make_candidate(engine), make_candidate(engine)

    result = run_role(role.id, role.job_id, [keep.id, lose.id])
    assert len(result.selected) == 2
    with engine.begin() as c:  # the consent is withdrawn between the run and the ask
        trust.withdraw_consent(c, lose.id, "open_to_introductions")

    outcome = worker(asks.ask_from_run, worker(asks.open_role, role.id), result.run_id)

    assert (outcome.asked, outcome.skipped, outcome.paused) == (1, 1, False)
    assert [i.user_id for i in introductions(engine, role)] == [keep.id]


def test_a_selection_whose_cited_fact_is_no_longer_usable_is_skipped(engine, provider):
    provider()
    role = make_role(engine)
    stale, fresh = make_candidate(engine), make_candidate(engine)

    result = run_role(role.id, role.job_id, [stale.id, fresh.id])
    with engine.begin() as c:
        c.execute(text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"), {"f": stale.record.fact_ids[1]})

    outcome = worker(asks.ask_from_run, worker(asks.open_role, role.id), result.run_id)

    assert (outcome.asked, outcome.skipped) == (1, 1)
    assert [i.user_id for i in introductions(engine, role)] == [fresh.id]


def test_a_run_older_than_a_day_is_not_used_for_asking(engine, provider):
    provider()
    role = make_role(engine)
    make_candidate(engine)

    result = run_role(role.id, role.job_id, [c for c in pool_of(engine, role)])
    with engine.begin() as c:
        c.execute(
            text("UPDATE match_runs SET finished_at = now() - interval '25 hours' WHERE id = :r"), {"r": result.run_id}
        )

    outcome = worker(asks.ask_from_run, worker(asks.open_role, role.id), result.run_id)

    assert (outcome.asked, outcome.skipped, outcome.paused) == (0, 0, False)
    assert introductions(engine, role) == []


def test_an_ask_is_private_no_shortlist_entry_and_no_employer_visible_row(engine, provider):
    provider()
    role = make_role(engine)
    make_candidate(engine)

    run_job(role)

    assert world.fetch(engine, "SELECT 1 FROM shortlist_entries") == []
    assert world.fetch(engine, "SELECT 1 FROM interest_signals") == []


# --- W2: the pool's predicates hold again when the ask is made from a run up to a day old ------------------------------


def run_then(engine, role, change):
    """Run the role against its pool, let `change` happen after the run, then ask from the run as the sweep does."""
    result = run_role(role.id, role.job_id, pool_of(engine, role))
    change()
    outcome = worker(asks.ask_from_run, worker(asks.open_role, role.id), result.run_id)
    return outcome


def test_a_candidate_who_withdrew_ai_processing_after_the_run_is_not_asked(engine, provider):
    provider()
    role = make_role(engine)
    ada, bob = make_candidate(engine), make_candidate(engine)

    def withdraw():
        with engine.begin() as c:
            trust.withdraw_consent(c, ada.id, "ai_processing")

    outcome = run_then(engine, role, withdraw)

    assert [i.user_id for i in introductions(engine, role)] == [bob.id]
    assert (outcome.asked, outcome.skipped) == (1, 1)


def test_a_candidate_who_joined_the_organization_after_the_run_is_not_asked(engine, provider):
    provider()
    role = make_role(engine)
    ada, bob = make_candidate(engine), make_candidate(engine)

    def join():
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO org_members (organization_id, user_id, role, status, joined_at) "
                    "VALUES (:o, :u, 'member', 'active', now())"
                ),
                {"o": role.org, "u": ada.id},
            )

    outcome = run_then(engine, role, join)

    assert [i.user_id for i in introductions(engine, role)] == [bob.id]
    assert (outcome.asked, outcome.skipped) == (1, 1)


def test_a_candidate_who_became_synthetic_is_not_asked_by_a_real_organization(engine, provider):
    provider()
    with engine.begin() as c:
        real = hiring.make_org(c, "Real Hiring Co", is_synthetic=False)
    role = make_role(engine, org=real)
    ada = make_candidate(engine, email="real.ada@example.com", is_synthetic=False)

    def becomes_synthetic():
        with engine.begin() as c:
            c.execute(text("UPDATE users SET is_synthetic = true WHERE id = :u"), {"u": ada.id})

    outcome = run_then(engine, role, becomes_synthetic)

    assert introductions(engine, role) == []
    assert (outcome.asked, outcome.skipped) == (0, 1)
