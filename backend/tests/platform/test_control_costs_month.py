"""08-03: the `month` filter of GET /v1/control/costs and `costs_by_campaign`. Invented ledger rows."""

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.platform import db
from app.platform.control_routes import costs_by_campaign, parse_month
from app.platform.sessions import Principal, current_user
from tests.campaigns.factories import make_world
from tests.conftest import _empty_tables
from tests.match.factories import make_user


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def add(engine, user, task, micro, when, campaign=None):
    with engine.begin() as conn:
        model = (
            conn.execute(text("SELECT id FROM model_registry WHERE model_name = 'recorded'")).scalar()
            or conn.execute(
                text(
                    "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
                    "VALUES ('fake', 'recorded', 'chat', true, 'x') RETURNING id"
                )
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id, input_tokens, "
                "output_tokens, cost_microusd, latency_ms, created_at) "
                "VALUES (:t, 'v1', :m, 'ok', :u, :c, 10, 5, :cost, 1, :w)"
            ),
            {
                "t": task,
                "m": model,
                "u": user,
                "c": campaign,
                "cost": micro,
                "w": datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
            },
        )


def client(user: UUID) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_user] = lambda: Principal(user)
    return TestClient(app)


@pytest.fixture
def person(clean):
    with clean.begin() as conn:
        a, b = make_user(conn), make_user(conn)
    add(clean, a, "ping", 1_000, "2026-10-31T23:59:59")  # the last second of October (UTC)
    add(clean, a, "ping", 2_000, "2026-11-01T00:00:00")  # the first of November
    add(clean, a, "other", 500, "2026-11-30T23:59:59")
    add(clean, a, "ping", 4_000, "2026-12-01T00:00:00")
    add(clean, b, "ping", 9_000_000, "2026-11-05T00:00:00")
    return a, b


def test_a_month_keeps_only_calls_of_that_utc_month(person):
    a, _ = person
    data = client(a).get("/v1/control/costs?group_by=task&month=2026-11").json()["data"]
    assert [(r["task"], r["calls"], r["cost_usd"]) for r in data["rows"]] == [
        ("ping", 1, "0.002000"), ("other", 1, "0.000500")
    ]  # fmt: skip
    assert (data["total_usd"], data["model_calls"]) == ("0.002500", 2)
    months = client(a).get("/v1/control/costs?group_by=month&month=2026-12").json()["data"]
    assert [(r["period"], r["cost_usd"]) for r in months["rows"]] == [("2026-12", "0.004000")]


def test_december_ends_at_the_new_year_and_a_month_without_calls_is_empty(person):
    a, _ = person
    assert client(a).get("/v1/control/costs?month=2027-01").json()["data"]["rows"] == []
    assert client(a).get("/v1/control/costs?month=2027-01").json()["data"]["total_usd"] == "0.000000"


def test_without_month_the_phase_one_answer_is_unchanged(person):
    a, _ = person
    data = client(a).get("/v1/control/costs?group_by=month").json()["data"]
    assert [(r["period"], r["calls"]) for r in data["rows"]] == [("2026-12", 1), ("2026-11", 2), ("2026-10", 1)]
    assert data["total_usd"] == "0.007500"


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "26-11", "2026-11-01", "november", ""])
def test_a_bad_month_is_422_validation_failed(person, month):
    a, _ = person
    response = client(a).get("/v1/control/costs", params={"month": month})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_another_persons_calls_are_not_counted_in_a_month(person):
    _, b = person
    assert client(b).get("/v1/control/costs?month=2026-11").json()["data"]["total_usd"] == "9.000000"


def test_costs_by_campaign_groups_per_campaign_and_one_row_for_none(clean):
    with clean.begin() as conn:
        w1, w2 = make_world(conn), make_world(conn)
    add(clean, w1.user, "t", 100, "2026-11-02T00:00:00", w1.campaign)
    add(clean, w1.user, "t", 200, "2026-10-02T00:00:00", w1.campaign)
    add(clean, w1.user, "t", 50, "2026-11-03T00:00:00")
    add(clean, w2.user, "t", 7_000, "2026-11-03T00:00:00", w2.campaign)
    with db.transaction(user_id=w1.user) as conn:
        lifetime = {r.campaign_id: (r.microusd, r.calls) for r in costs_by_campaign(conn)}
        november = {r.campaign_id: (r.microusd, r.calls) for r in costs_by_campaign(conn, month=date(2026, 11, 1))}
    assert lifetime == {w1.campaign: (300, 2), None: (50, 1)}  # the second person's campaign is not seen
    assert november == {w1.campaign: (100, 1), None: (50, 1)}


def test_parse_month():
    assert parse_month(None) is None
    assert parse_month("2026-11") == date(2026, 11, 1)
