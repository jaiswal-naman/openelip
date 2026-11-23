"""08-03: GET /v1/control/runs. Every row is invented (campaigns and match factories), committed,
and read through the real app on the API login with row-level security on."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import count
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.campaigns import control_routes
from app.main import create_app
from app.platform import config, db
from app.platform.sessions import Principal, current_user
from tests.campaigns.factories import make_action, make_campaign, make_run, make_world
from tests.conftest import _empty_tables
from tests.match import factories as match_factories

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def client(user: UUID | None) -> TestClient:
    app = create_app()
    if user:
        app.dependency_overrides[current_user] = lambda: Principal(user)
    return TestClient(app)


def start_at(engine, run_id, when, finished=None):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_runs SET started_at = :s, finished_at = :f WHERE id = :id"),
            {"s": when, "f": finished, "id": run_id},
        )


def add_run(engine, w, role="researcher", *, minutes=0, status="running", took=None):
    with engine.begin() as conn:
        run = make_run(conn, w.user, w.campaign, role)
    when = T0 + timedelta(minutes=minutes)
    start_at(engine, run, when, None)
    if status != "running":
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE agent_runs SET status = CAST(:s AS agent_run_status), finished_at = :f WHERE id = :id"),
                {"s": status, "f": when + (took or timedelta(seconds=30)), "id": run},
            )
    return run


def add_cost(engine, user, campaign, run, micro):
    with engine.begin() as conn:
        model = conn.execute(
            text(
                "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
                "VALUES ('local', 'm-' || gen_random_uuid(), 'chat', true, 'x') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id, agent_run_id, "
                "cost_microusd, latency_ms) VALUES ('t', 'v1', :m, 'ok', :u, :c, :r, :cost, 1)"
            ),
            {"m": model, "u": user, "c": campaign, "r": run, "cost": micro},
        )


_days = count()


def add_match_run(engine, user, *, minutes=0, status="completed", trigger="nightly"):
    day = date(2026, 1, 1) + timedelta(days=next(_days))  # one nightly run per person and local date
    with engine.begin() as conn:
        run = match_factories.make_run(conn, user, trigger=trigger, status=status, local_date=day)
        conn.execute(
            text("UPDATE match_runs SET started_at = :s WHERE id = :id"),
            {"s": T0 + timedelta(minutes=minutes), "id": run},
        )
    return run


def world(engine):
    with engine.begin() as conn:
        return make_world(conn)


def get(user, **params):
    response = client(user).get("/v1/control/runs", params=params)
    return response


def test_lists_agent_runs_and_scout_runs_newest_first_with_cost_blocked_and_duration(clean):
    w = world(clean)
    a = add_run(clean, w, "researcher", minutes=0, status="succeeded", took=timedelta(seconds=90))
    b = add_run(clean, w, "chaser", minutes=10, status="failed")
    c = add_run(clean, w, "writer", minutes=20)
    add_cost(clean, w.user, w.campaign, a, 12_345)
    add_cost(clean, w.user, w.campaign, a, 5)
    with clean.begin() as conn:
        make_action(conn, a, kind="outside_action", status="blocked")
        make_action(conn, a, kind="outside_action", status="blocked")
        make_action(conn, a, kind="model_call", status="recorded")
    m = add_match_run(clean, w.user, minutes=15, trigger="nightly")

    body = get(w.user).json()
    assert body["success"] is True
    data = body["data"]
    assert [r["id"] for r in data["runs"]] == [str(c), str(m), str(b), str(a)]
    by_id = {r["id"]: r for r in data["runs"]}
    first = by_id[str(a)]
    assert first["source"] == "campaign"
    assert first["campaign"] == {"id": str(w.campaign), "title": "Data engineer", "company_name": "Lumen Demo"}
    assert (first["agent_role"], first["status"], first["blocked"], first["cost_usd"]) == (
        "researcher", "succeeded", 2, "0.012350",
    )  # fmt: skip
    assert first["duration_ms"] == 90_000 and first["trigger"] is None
    assert by_id[str(c)]["status"] == "running" and by_id[str(c)]["duration_ms"] is None
    assert by_id[str(c)]["cost_usd"] == "0.000000"  # a campaign run with no calls cost nothing: zero, not unknown
    scout = by_id[str(m)]
    assert (scout["source"], scout["agent_role"], scout["status"], scout["trigger"]) == (
        "matching", "scout", "succeeded", "nightly",
    )  # fmt: skip
    assert scout["campaign"] is None and scout["cost_usd"] is None and scout["blocked"] == 0
    assert scout["duration_ms"] is not None
    assert data["next_cursor"] is None and data["server_time"]


def test_a_running_match_run_is_running_and_a_failed_one_failed(clean):
    w = world(clean)
    running = add_match_run(clean, w.user, minutes=1, status="running")
    failed = add_match_run(clean, w.user, minutes=2, status="failed", trigger="on_demand")
    got = {r["id"]: r for r in get(w.user).json()["data"]["runs"]}
    assert (got[str(running)]["status"], got[str(running)]["duration_ms"]) == ("running", None)
    assert (got[str(failed)]["status"], got[str(failed)]["trigger"]) == ("failed", "on_demand")


def test_a_guardian_control_run_is_never_listed_with_or_without_filters(clean):
    w = world(clean)
    with clean.begin() as conn:
        control = make_run(conn, w.user, w.campaign, "guardian")
    real = add_run(clean, w, "operator", minutes=5)
    for params in ({}, {"campaign_id": str(w.campaign)}, {"status": "running"}, {"agent_role": "operator"}):
        ids = [r["id"] for r in get(w.user, **params).json()["data"]["runs"]]
        assert str(control) not in ids
        assert str(real) in ids
    assert get(w.user, agent_role="guardian").status_code == 422


def test_filters_by_campaign_role_and_status(clean):
    w = world(clean)
    with clean.begin() as conn:  # a second campaign of the same person
        job = match_factories.make_job(
            conn,
            title="Analyst",
            company_name="Initech Demo",
            description="Needs SQL.",
            requirements=[("SQL", "required")],
        )
        other_campaign = w._replace(campaign=make_campaign(conn, w.user, job.id))
    r1 = add_run(clean, w, "researcher", minutes=1, status="succeeded")
    r2 = add_run(clean, w, "chaser", minutes=2, status="failed")
    r3 = add_run(clean, other_campaign, "researcher", minutes=3)  # the same person, another campaign
    m1 = add_match_run(clean, w.user, minutes=4, status="completed")
    m2 = add_match_run(clean, w.user, minutes=5, status="failed")

    def ids(**params):
        return {r["id"] for r in get(w.user, **params).json()["data"]["runs"]}

    assert ids(campaign_id=str(other_campaign.campaign)) == {str(r3)}
    assert ids(campaign_id=str(w.campaign)) == {str(r1), str(r2)}  # no matching runs: they have no campaign
    assert ids(agent_role="scout") == {str(m1), str(m2)}
    assert ids(agent_role="researcher") == {str(r1), str(r3)}
    assert ids(status="succeeded") == {str(r1), str(m1)}  # a completed match run is a succeeded run
    assert ids(status="failed") == {str(r2), str(m2)}
    assert ids(status="running") == {str(r3)}
    assert ids(status="cancelled") == set()
    assert ids(agent_role="scout", status="failed") == {str(m2)}


@pytest.mark.parametrize(
    "params", [{"agent_role": "wizard"}, {"status": "done"}, {"limit": 0}, {"limit": 101}, {"campaign_id": "nope"}]
)
def test_a_bad_filter_is_422_validation_failed(clean, params):
    w = world(clean)
    response = get(w.user, **params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize("cursor", ["x", "e30", "not-base64!!", "WyJ4Il0"])
def test_a_malformed_cursor_is_422(clean, cursor):
    w = world(clean)
    response = get(w.user, cursor=cursor)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_paging_follows_the_cursor_without_repeating_or_skipping_equal_start_times(clean):
    w = world(clean)
    runs = [add_run(clean, w, "researcher", minutes=0) for _ in range(4)]  # four runs share one start time
    runs += [add_run(clean, w, "chaser", minutes=m) for m in (5, 6)]
    matches = [add_match_run(clean, w.user, minutes=m) for m in (0, 6, 7)]
    expected = len(runs) + len(matches)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        data = get(w.user, **params).json()["data"]
        seen += [r["id"] for r in data["runs"]]
        pages += 1
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == expected == len(set(seen))
    assert pages == 5
    assert {str(r) for r in runs + matches} == set(seen)
    whole = [r["id"] for r in get(w.user, limit=100).json()["data"]["runs"]]
    assert seen == whole  # the same order as one page


def test_another_persons_runs_never_appear_and_their_campaign_id_gives_an_empty_list(clean):
    mine, theirs = world(clean), world(clean)
    add_run(clean, mine, "researcher", minutes=1)
    secret = add_run(clean, theirs, "researcher", minutes=2)
    add_cost(clean, theirs.user, theirs.campaign, secret, 9_000_000)
    add_match_run(clean, theirs.user, minutes=3)
    data = get(mine.user).json()["data"]
    assert len(data["runs"]) == 1
    assert str(secret) not in [r["id"] for r in data["runs"]]
    assert get(mine.user, campaign_id=str(theirs.campaign)).json()["data"]["runs"] == []
    theirs_listed = {r["id"]: r for r in get(theirs.user).json()["data"]["runs"]}
    assert theirs_listed[str(secret)]["cost_usd"] == "9.000000"
    assert get(mine.user, campaign_id=str(uuid4())).json()["data"]["runs"] == []


def test_a_cursor_from_another_persons_list_only_positions_the_callers_own_rows(clean):
    mine, theirs = world(clean), world(clean)
    mine_run = add_run(clean, mine, "researcher", minutes=1)
    for m in (5, 6, 7):
        add_run(clean, theirs, "researcher", minutes=m)
    page = get(theirs.user, limit=1).json()["data"]
    assert page["next_cursor"]
    rows = get(mine.user, cursor=page["next_cursor"]).json()["data"]["runs"]
    assert [r["id"] for r in rows] == [str(mine_run)]


def test_signed_out_is_401(clean):
    assert client(None).get("/v1/control/runs").status_code == 401


def test_the_handlers_are_plain_def():
    import inspect

    assert not inspect.iscoroutinefunction(control_routes.read_runs)
    assert not inspect.iscoroutinefunction(control_routes.campaign_costs)


def test_the_runs_list_needs_only_the_api_login(clean, monkeypatch):
    w = world(clean)
    run = add_run(clean, w, "researcher", minutes=1)
    add_match_run(clean, w.user, minutes=2)
    changed = replace(config.get(), database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})
    try:
        response = get(w.user)
        assert response.status_code == 200
        assert str(run) in [r["id"] for r in response.json()["data"]["runs"]]
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
