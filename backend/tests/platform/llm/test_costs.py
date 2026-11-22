"""GET /v1/control/costs: a person sees their own ledger rows and nobody else's (T-01-18)."""

from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.platform.sessions import Principal, current_user


@pytest.fixture
def ledger(harness):
    with harness.engine.begin() as conn:
        model = conn.execute(text(
            "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
            "VALUES ('fake', 'recorded', 'chat', true, 'x') RETURNING id")).scalar_one()  # fmt: skip

    def add(user: UUID, task: str, micro: int, when: str) -> None:
        with harness.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, input_tokens, "
                    "output_tokens, cost_microusd, latency_ms, created_at) "
                    "VALUES (:t, 'v1', :m, 'ok', :u, 10, 5, :c, 1, :w)"
                ),
                {
                    "t": task,
                    "m": model,
                    "u": user,
                    "c": micro,
                    "w": datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
                },
            )

    return add


def client(user: UUID | None) -> TestClient:
    app = create_app()
    if user:
        app.dependency_overrides[current_user] = lambda: Principal(user)
    return TestClient(app)


def test_costs_by_month_sum_only_the_callers_rows(harness, ledger):
    a, b = harness.user("a@example.test"), harness.user("b@example.test")
    ledger(a, "ping", 1500, "2026-10-03")
    ledger(a, "ping", 2000, "2026-11-02")
    ledger(a, "other", 500, "2026-11-20")
    ledger(b, "ping", 9_000_000, "2026-11-05")
    data = client(a).get("/v1/control/costs?group_by=month").json()["data"]
    assert data["rows"] == [
        {
            "period": "2026-11",
            "task": None,
            "calls": 2,
            "cost_usd": "0.002500",
            "input_tokens": 20,
            "output_tokens": 10,
        },
        {"period": "2026-10", "task": None, "calls": 1, "cost_usd": "0.001500", "input_tokens": 10, "output_tokens": 5},
    ]
    assert (data["total_usd"], data["model_calls"], data["currency"], data["group_by"]) == (
        "0.004000",
        3,
        "USD",
        "month",
    )
    assert data["label"] == "Measured: sums of recorded model calls."
    assert client(b).get("/v1/control/costs").json()["data"]["total_usd"] == "9.000000"  # the default is month


def test_costs_by_task_group_per_task_most_expensive_first(harness, ledger):
    a = harness.user()
    ledger(a, "ping", 100, "2026-11-01")
    ledger(a, "ping", 100, "2026-11-02")
    ledger(a, "other", 900, "2026-11-03")
    rows = client(a).get("/v1/control/costs?group_by=task").json()["data"]["rows"]
    assert [(r["task"], r["period"], r["calls"], r["cost_usd"]) for r in rows] == [
        ("other", None, 1, "0.000900"), ("ping", None, 2, "0.000200")
    ]  # fmt: skip


def test_an_empty_ledger_is_an_empty_list_not_an_error(harness):
    data = client(harness.user()).get("/v1/control/costs").json()["data"]
    assert (data["rows"], data["total_usd"], data["model_calls"]) == ([], "0.000000", 0)


def test_an_unknown_group_is_a_validation_error_naming_the_field(harness):
    response = client(harness.user()).get("/v1/control/costs?group_by=campaign")
    assert response.status_code == 422 and response.json()["error"]["details"]["fields"][0]["field"] == "group_by"


def test_without_a_session_the_answer_is_401(harness):
    assert client(None).get("/v1/control/costs").status_code == 401
