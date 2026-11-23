"""Plan 07-03, GRD-07: spend is the sum of the ledger by campaign and by person and calendar month (UTC), and a budget
that spend plus the estimate would pass is not ok."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import budget
from app.guardian.policy import SubjectGate
from app.platform import config, db
from tests.guardian.support import add_spend, make_committed_world


def gate_of(world, budget_microusd=1_000_000):
    return SubjectGate(
        "campaign", world.campaign, world.user, True, None, False, world.job_id, None, None, budget_microusd
    )


def test_spend_sums_by_campaign_by_person_and_by_month(engine, clean):
    world = make_committed_world(engine)
    other = make_committed_world(engine, consent=False)
    add_spend(engine, world, 100_000)
    add_spend(engine, world, 50_000)
    add_spend(engine, world, 7_000, campaign=False)
    add_spend(engine, other, 900_000)
    last_month = add_spend(engine, world, 400_000)
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("ALTER TABLE llm_calls DISABLE TRIGGER USER"))
        c.execute(
            text("UPDATE llm_calls SET created_at = date_trunc('month', now()) - interval '1 day' WHERE id = :i"),
            {"i": last_month},
        )
        c.execute(text("ALTER TABLE llm_calls ENABLE TRIGGER USER"))
    with db.transaction(world.user, role="worker") as conn:
        assert guardian.spent_microusd(conn, campaign_id=world.campaign) == 550_000
        assert guardian.spent_microusd(conn, user_id=world.user) == 557_000
        assert guardian.spent_microusd(conn, user_id=world.user, month=budget.current_month()) == 157_000
        previous = budget.current_month() - timedelta(days=1)
        assert guardian.spent_microusd(conn, user_id=world.user, month=previous) == 400_000
        assert guardian.spent_microusd(conn, campaign_id=uuid4()) == 0
        assert guardian.spent_microusd(conn) == 1_457_000


def test_the_month_starts_on_the_first_in_utc():
    now = datetime.now(timezone.utc)
    assert budget.current_month() == date(now.year, now.month, 1)


@pytest.mark.parametrize(
    ("spent", "estimate", "ok"),
    [
        (0, 10_000, True),
        (900_000, 100_000, True),
        (900_000, 100_001, False),
        (1_000_000, 0, False),
        (1_200_000, 0, False),
    ],
)
def test_budget_ok_is_false_when_spend_plus_the_estimate_passes_the_campaign_budget(engine, clean, spent, estimate, ok):
    world = make_committed_world(engine)
    if spent:
        add_spend(engine, world, spent)
    with db.transaction(world.user, role="worker") as conn:
        assert guardian.budget_ok(conn, gate_of(world), estimate) is ok


def test_budget_ok_is_false_when_the_monthly_budget_would_be_passed(engine, clean, monkeypatch):
    world = make_committed_world(engine)
    add_spend(engine, world, 4_950_000, campaign=False)  # the person's month: 4.95 of 5 dollars
    with db.transaction(world.user, role="worker") as conn:
        assert config.get().monthly_budget_microusd == 5_000_000
        assert guardian.budget_ok(conn, gate_of(world), 40_000) is True
        assert guardian.budget_ok(conn, gate_of(world), 60_000) is False


def test_a_campaign_over_budget_fails_the_budget_check_and_blocks_a_proposal(engine, clean, monkeypatch, tmp_path):
    from tests.guardian.support import entails, propose_follow_up

    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE campaigns SET budget_microusd = 100_000"))
    add_spend(engine, world, 100_000)
    proposed = propose_follow_up(world)
    assert (proposed.status, proposed.failed_codes) == ("blocked", ("budget_exhausted",))
