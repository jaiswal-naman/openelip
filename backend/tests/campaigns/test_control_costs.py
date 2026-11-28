"""08-03: GET /v1/control/costs/campaigns. Invented rows, read through the real app on the API login."""

from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.platform import config, db
from app.platform.control_routes import LABEL
from app.platform.sessions import Principal, current_user
from tests.campaigns.factories import make_campaign, make_world
from tests.conftest import _empty_tables
from tests.match.factories import make_job

UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def add(engine, user, micro, when, campaign=None):
    with engine.begin() as conn:
        model = (
            conn.execute(text("SELECT id FROM model_registry LIMIT 1")).scalar()
            or conn.execute(
                text(
                    "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
                    "VALUES ('fake', 'recorded', 'chat', true, 'x') RETURNING id"
                )
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id, cost_microusd, "
                "latency_ms, created_at) VALUES ('t', 'v1', :m, 'ok', :u, :c, :cost, 1, :w)"
            ),
            {
                "m": model,
                "u": user,
                "c": campaign,
                "cost": micro,
                "w": datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
            },
        )


def get(user: UUID | None, **params):
    app = create_app()
    if user:
        app.dependency_overrides[current_user] = lambda: Principal(user)
    return TestClient(app).get("/v1/control/costs/campaigns", params=params)


def second_campaign(engine, w, title, *, stage="researching", budget=750_000):
    with engine.begin() as conn:
        job = make_job(
            conn, title=title, company_name="Initech Demo", description="Needs SQL.", requirements=[("SQL", "required")]
        )
        return make_campaign(conn, w.user, job.id, stage=stage, budget_microusd=budget)


def world(engine):
    with engine.begin() as conn:
        return make_world(conn)


def test_each_open_campaign_with_spend_budget_and_the_not_tied_group(clean):
    w = world(clean)
    quiet = second_campaign(clean, w, "Quiet role", budget=2_000_000)
    add(clean, w.user, 120_000, "2026-11-02T00:00:00", w.campaign)
    add(clean, w.user, 30_000, "2026-10-02T00:00:00", w.campaign)
    add(clean, w.user, 4_000, "2026-11-03T00:00:00")  # a call tied to no campaign
    data = get(w.user).json()["data"]
    assert data["month"] is None and data["currency"] == "USD" and data["label"] == LABEL
    first, second, last = data["groups"]
    assert (first["campaign_id"], first["title"], first["company_name"], first["stage"]) == (
        str(w.campaign), "Data engineer", "Lumen Demo", "researching",
    )  # fmt: skip
    assert (first["spent_usd"], first["lifetime_spent_usd"], first["budget_usd"], first["model_calls"]) == (
        "0.150000", "0.150000", "0.750000", 2,
    )  # fmt: skip
    assert (second["campaign_id"], second["spent_usd"], second["budget_usd"], second["model_calls"]) == (
        str(quiet), "0.000000", "2.000000", 0,
    )  # fmt: skip
    assert (last["campaign_id"], last["title"], last["budget_usd"], last["stage"]) == (
        None, "Not tied to a campaign", None, None,
    )  # fmt: skip
    assert (last["spent_usd"], last["model_calls"]) == ("0.004000", 1)
    assert data["total_usd"] == "0.154000" and data["model_calls"] == 3


def test_a_month_gives_the_months_spend_beside_the_lifetime_spend(clean):
    w = world(clean)
    add(clean, w.user, 120_000, "2026-11-02T00:00:00", w.campaign)
    add(clean, w.user, 30_000, "2026-10-02T00:00:00", w.campaign)
    data = get(w.user, month="2026-10").json()["data"]
    assert data["month"] == "2026-10"
    group = data["groups"][0]
    assert (group["spent_usd"], group["lifetime_spent_usd"], group["model_calls"]) == ("0.030000", "0.150000", 1)
    assert (data["total_usd"], data["model_calls"]) == ("0.030000", 1)


def test_totals_equal_the_sum_of_the_groups_and_order_is_by_spend(clean):
    w = world(clean)
    big = second_campaign(clean, w, "Big spender")
    add(clean, w.user, 1_000, "2026-11-02T00:00:00", w.campaign)
    add(clean, w.user, 5_000, "2026-11-02T00:00:00", big)
    add(clean, w.user, 25, "2026-11-02T00:00:00")
    data = get(w.user, month="2026-11").json()["data"]
    assert [g["campaign_id"] for g in data["groups"]] == [str(big), str(w.campaign), None]
    micro = sum(int(g["spent_usd"].replace(".", "")) for g in data["groups"])
    assert int(data["total_usd"].replace(".", "")) == micro == 6_025
    assert sum(g["model_calls"] for g in data["groups"]) == data["model_calls"] == 3


def test_a_closed_campaign_shows_only_when_it_has_spend_in_the_period(clean):
    w = world(clean)
    closed = second_campaign(clean, w, "Closed role", stage="closed")
    add(clean, w.user, 800, "2026-10-05T00:00:00", closed)
    assert closed not in [g["campaign_id"] for g in get(w.user, month="2026-11").json()["data"]["groups"]]
    in_oct = {g["campaign_id"]: g for g in get(w.user, month="2026-10").json()["data"]["groups"]}
    assert in_oct[str(closed)]["spent_usd"] == "0.000800" and in_oct[str(closed)]["stage"] == "closed"


def test_another_persons_campaigns_and_calls_are_never_shown_or_counted(clean):
    mine, theirs = world(clean), world(clean)
    add(clean, mine.user, 100, "2026-11-02T00:00:00", mine.campaign)
    add(clean, theirs.user, 9_000_000, "2026-11-02T00:00:00", theirs.campaign)
    add(clean, theirs.user, 8_000_000, "2026-11-02T00:00:00")
    data = get(mine.user).json()["data"]
    ids = [g["campaign_id"] for g in data["groups"]]
    assert str(theirs.campaign) not in ids and ids == [str(mine.campaign), None]
    assert data["total_usd"] == "0.000100"
    assert data["groups"][-1]["spent_usd"] == "0.000000"  # nothing of the caller's is unattached


def test_no_calls_at_all_shows_zero_strings_never_null(clean):
    w = world(clean)
    data = get(w.user).json()["data"]
    assert data["total_usd"] == "0.000000" and data["model_calls"] == 0
    assert all(g["spent_usd"] == "0.000000" and g["lifetime_spent_usd"] == "0.000000" for g in data["groups"])


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "202611", "x"])
def test_a_bad_month_is_422(clean, month):
    w = world(clean)
    response = get(w.user, month=month)
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_failed"


def test_signed_out_is_401(clean):
    assert get(None).status_code == 401


def test_the_costs_need_only_the_api_login(clean, monkeypatch):
    w = world(clean)
    add(clean, w.user, 100, "2026-11-02T00:00:00", w.campaign)
    changed = replace(config.get(), database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})
    try:
        for month in (None, "2026-11"):
            response = get(w.user, **({"month": month} if month else {}))
            assert response.status_code == 200
            assert response.json()["data"]["total_usd"] == "0.000100"
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
