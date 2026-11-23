"""HIR-07 (plan 06-05, task 2): a candidate who blocked an employer is never asked by it, and never scored for its role.

The block is the candidate's hard constraint excluded_company (Phase 3). The role run applies it first, in SQL, before
any model call; the database refuses the ask itself as the last line of defence. Everything is invented.
"""

import pytest

from app.hiring import ask_jobs
from app.match.api import run_role
from tests.hiring import asks_world
from tests.hiring import factories as hiring
from tests.hiring import world as hiring_world
from tests.hiring.asks_world import introductions, make_candidate, make_role, worker
from tests.match.fixtures.run import world


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    yield from asks_world.committed(engine, monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    return asks_world.provider(monkeypatch)


def decisions_for(engine, role, user):
    return world.fetch(
        engine,
        "SELECT 1 FROM match_decisions WHERE job_id = :job AND user_id = :u",
        job=role.job_id,
        u=user,
    )


def test_a_candidate_who_blocked_the_organization_before_the_role_was_published_is_never_asked_or_scored(
    engine, provider
):
    calls = provider()
    with engine.begin() as c:
        org = hiring.make_org(c, "Blocked Demo")
        blocker = hiring.make_candidate(c)
        hiring_world.exclude_company(c, blocker.id, "Blocked Demo")
    role = make_role(engine, org=org)
    open_person = make_candidate(engine)

    worker(ask_jobs.ask_candidates, {"role_id": str(role.id)})

    assert [i.user_id for i in introductions(engine, role)] == [open_person.id]
    assert decisions_for(engine, role, blocker.id) == []  # not even a withheld row: filtered candidates are counted
    assert len(calls) == 1  # one model call, for the one candidate who was not filtered


def test_a_candidate_who_blocks_after_a_first_run_is_excluded_from_the_next_run(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    first = run_role(role.id, role.job_id, [ada.id])
    assert len(first.selected) == 1
    with engine.begin() as c:
        hiring_world.exclude_company(c, ada.id, role.company)

    second = run_role(role.id, role.job_id, [ada.id])

    assert (second.filtered, second.scored, second.selected) == (1, 0, [])


def test_a_forced_ask_for_a_candidate_who_blocked_the_organization_is_refused_by_the_database(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        decision = hiring.make_introduced_decision(c, role, ada)
        hiring_world.exclude_company(c, ada.id, role.company)

    with pytest.raises(Exception) as refused:
        with engine.begin() as c:
            hiring.make_ask(c, role, ada, decision=decision)

    assert "hard_constraint" in str(refused.value.orig.diag.message_primary)
    assert introductions(engine, role) == []
    assert world.fetch(engine, "SELECT 1 FROM introductions") == []
