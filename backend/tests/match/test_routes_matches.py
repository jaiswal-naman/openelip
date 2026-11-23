"""MAT-02, MAT-03, MAT-04, MAT-07: GET /v1/matches and GET /v1/matches/{decision_id} (plan 04-04).

The API opens its own connections as the real app_api role, so row-level security and the grants are in force and what a
test commits stays: these tests use api_client (it empties the tables around each test), write their rows with the
04-01 factories on a committing connection, and sign a person in by overriding the session dependency, as the record
routes' tests do. Everything here is invented. The run pipeline (plan 04-03) is not used: the rows are built by hand.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.match import ledger
from app.platform.sessions import Principal, current_user
from app.profile import service
from tests.match.factories import (
    LOCAL_DATE,
    RESUME_TEXT,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

DAY = LOCAL_DATE.isoformat()
TOMORROW = LOCAL_DATE + timedelta(days=1)
DESCRIPTION = "Build data pipelines in Python and SQL. Kubernetes is a plus."
GO_DESCRIPTION = "Run the platform. Ten years of Go required."


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def seed(engine) -> SimpleNamespace:
    """Ada, with a confirmed record, a fact she wrote herself and two shown matches on DAY, one withheld job on the same
    day and one shown match tomorrow; Bob, with a shown match of his own on the first job."""
    w = SimpleNamespace()
    with engine.begin() as conn:
        w.ada, w.bob = make_user(conn), make_user(conn)
        w.record, w.bob_record = make_record(conn, w.ada), make_record(conn, w.bob)
        w.written = UUID(service.add_fact(conn, w.ada, "skill", "Go for six years, self-taught").row["id"])
        w.data = make_job(
            conn,
            title="Data engineer",
            description=DESCRIPTION,
            requirements=[("Python and SQL", "required"), ("Kubernetes", "preferred")],
            country="IN",
            city="Bengaluru",
            arrangements=["hybrid"],
        )
        w.platform = make_job(
            conn,
            title="Platform engineer",
            description=GO_DESCRIPTION,
            requirements=[("Ten years of Go required", "required")],
            company_name="Hooli Demo",
            country="IN",
            arrangements=["remote"],
            salary_min=3_000_000,
            salary_max=4_000_000,
            salary_currency="INR",
        )
        w.capped = make_job(
            conn, title="Analyst", description="Analyse data.", requirements=[("Analyse data", "required")]
        )
        w.run = make_run(conn, w.ada, retrieved_count=40, filtered_count=388)
        w.first = make_shown_decision(
            conn,
            w.run,
            w.ada,
            w.data,
            [Verdict("met", w.record.fact_ids[1:2]), Verdict("no_evidence")],
            rank_position=1,
            summary="Meets 1 of 1 required item.",
            unverified=("salary_not_stated",),
        )
        w.second = make_shown_decision(
            conn,
            w.run,
            w.ada,
            w.platform,
            [Verdict("partly_met", (w.written,))],
            rank_position=2,
            summary="Meets 0 of 1 required item.",
            change_note="Pay is now stated.",
        )
        w.held = make_decision(
            conn, w.run, w.ada, w.capped, outcome="withheld", reasons=("below_daily_cap",), retrieval_rank=3
        )
        w.tomorrow_run = make_run(conn, w.ada, local_date=TOMORROW)
        w.tomorrow = make_shown_decision(
            conn, w.tomorrow_run, w.ada, w.capped, [Verdict("no_evidence")], rank_position=1, summary="Later."
        )
        w.bob_run = make_run(conn, w.bob)
        w.bobs = make_shown_decision(
            conn,
            w.bob_run,
            w.bob,
            w.data,
            [Verdict("no_evidence"), Verdict("no_evidence")],
            rank_position=1,
            summary="Bob's.",
        )
    return w


@pytest.fixture
def world(api_client, engine) -> SimpleNamespace:
    w = seed(engine)
    w.client = api_client
    w.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    w.sign_in(w.ada)
    return w


def as_text(value) -> str:
    return str(value)


# ---- GET /v1/matches ---------------------------------------------------------------------------------------------


def test_todays_matches_are_the_shown_decisions_of_the_date_ranked_with_the_run_and_every_requirement(world):
    data = ok(world.client.get("/v1/matches", params={"date": DAY}))

    assert (data["local_date"], data["timezone"]) == (DAY, "Asia/Kolkata")
    assert data["run"] == {
        "id": str(world.run),
        "record_version": 2,  # the fact Ada wrote herself moved the record on before the run read it
        "shown": 2,
        "withheld": 1,
        "filtered": 388,
        "checker": "passed",
    }
    first, second = data["matches"]  # the withheld job, tomorrow's match and Bob's match are not here
    assert [m["decision_id"] for m in data["matches"]] == [str(world.first), str(world.second)]
    assert (first["rank_position"], first["reason_codes"], first["change_note"]) == (1, ["top_ranked"], None)
    assert (first["summary"], first["unverified"]) == ("Meets 1 of 1 required item.", ["salary_not_stated"])
    assert (second["rank_position"], second["reason_codes"]) == (2, ["top_ranked", "material_change"])
    assert (second["change_note"], second["unverified"]) == ("Pay is now stated.", [])

    job = first["job"]
    assert (job["id"], job["title"], job["company_name"], job["status"]) == (
        world.data.id,
        "Data engineer",
        "Initech Demo",
        "open",
    )
    assert (job["country"], job["city"], job["work_arrangements"], job["is_remote"]) == (
        "IN",
        "Bengaluru",
        ["hybrid"],
        False,
    )
    assert job["pay"] is None and job["content_version"] == 1 and job["apply_url"].startswith("https://example.test/")
    assert job["source"] == {"ats": "lever", "board": "initech-demo"} and job["last_verified_at"].endswith("Z")
    assert second["job"]["pay"] == {"min": 3_000_000, "max": 4_000_000, "currency": "INR"}
    assert second["job"]["is_remote"] is True

    assert [e["requirement"]["text"] for e in first["evidence"]] == ["Python and SQL", "Kubernetes"]  # every one, once
    python, kubernetes = first["evidence"]
    assert python["requirement"]["id"] == world.data.requirement_ids[0]
    assert python["requirement"]["importance"] == "required" and kubernetes["requirement"]["importance"] == "preferred"
    start = DESCRIPTION.index("Python and SQL")
    assert python["requirement"]["job_citation"] == {
        "span": {"start": start, "end": start + len("Python and SQL")},
        "quote": "Python and SQL",
    }
    assert (python["verdict"], kubernetes["verdict"]) == ("met", "no_evidence")
    assert kubernetes["record_citations"] == []
    (citation,) = python["record_citations"]
    fact_text = "Built nightly Python and SQL pipelines that cut report time by 40 percent."
    start = RESUME_TEXT.index(fact_text)
    assert citation == {
        "fact_id": str(world.record.fact_ids[1]),
        "quote": fact_text,
        "source": {"document_id": str(world.record.document_id), "span": {"start": start, "end": start + len(fact_text)}},
        "is_current": True,
    }
    assert first["feedback"] == {"is_saved": False, "is_dismissed": False}


def test_a_fact_the_person_wrote_has_no_source_and_its_own_words_as_the_quote(world):
    data = ok(world.client.get("/v1/matches", params={"date": DAY}))
    (citation,) = data["matches"][1]["evidence"][0]["record_citations"]
    assert citation == {
        "fact_id": str(world.written),
        "quote": "Go for six years, self-taught",
        "source": None,
        "is_current": True,
    }
    assert data["matches"][1]["evidence"][0]["verdict"] == "partly_met"


def test_a_fact_corrected_since_shows_its_original_quote_and_is_no_longer_current(world, engine):
    with engine.begin() as conn:
        service.correct_fact(conn, world.ada, world.record.fact_ids[1], "Built pipelines in Python.")
        service.reject_fact(conn, world.ada, world.written)  # a confirmed fact that is rejected is retired
    data = ok(world.client.get("/v1/matches", params={"date": DAY}))
    (cited,) = data["matches"][0]["evidence"][0]["record_citations"]
    assert cited["fact_id"] == str(world.record.fact_ids[1]) and cited["is_current"] is False
    assert cited["quote"].startswith("Built nightly Python and SQL pipelines")  # what the verdict was made on
    (retired,) = data["matches"][1]["evidence"][0]["record_citations"]
    assert retired["fact_id"] == str(world.written) and retired["is_current"] is False
    assert retired["quote"] == "Go for six years, self-taught"


def test_a_day_with_a_finished_run_and_nothing_shown_is_an_empty_list_with_the_run_summary(world, engine):
    day = LOCAL_DATE + timedelta(days=5)
    with engine.begin() as conn:
        run = make_run(conn, world.ada, local_date=day, retrieved_count=12, filtered_count=9)
        make_decision(conn, run, world.ada, world.capped, outcome="withheld", reasons=("weak_fit",))
    data = ok(world.client.get("/v1/matches", params={"date": day.isoformat()}))
    assert data["matches"] == []
    assert data["run"] == {
        "id": str(run),
        "record_version": 2,
        "shown": 0,
        "withheld": 1,
        "filtered": 9,
        "checker": "passed",
    }


def test_a_day_nobody_ran_has_no_run_and_no_matches(world):
    data = ok(world.client.get("/v1/matches", params={"date": "2026-01-01"}))
    assert (data["run"], data["matches"], data["local_date"]) == (None, [], "2026-01-01")


def test_a_run_that_did_not_finish_is_not_the_days_run(world, engine):
    day = LOCAL_DATE + timedelta(days=6)
    with engine.begin() as conn:
        make_run(conn, world.ada, local_date=day, status="running")
        make_run(conn, world.ada, local_date=day, trigger="on_demand", status="failed")
    assert ok(world.client.get("/v1/matches", params={"date": day.isoformat()}))["run"] is None


def test_the_run_of_the_day_is_the_latest_one_that_finished_and_says_when_the_checker_disagreed(world, engine):
    with engine.begin() as conn:
        later = make_run(
            conn,
            world.ada,
            trigger="on_demand",
            filtered_count=5,
            finished_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        conn.execute(
            text("UPDATE match_runs SET error_code = 'checker_disagreement' WHERE id = :id"), {"id": later}
        )
    run = ok(world.client.get("/v1/matches", params={"date": DAY}))["run"]
    assert (run["id"], run["filtered"], run["checker"]) == (str(later), 5, "disagreed")


def test_without_a_date_the_list_is_the_callers_local_today(world, monkeypatch):
    """Ada's time zone is Asia/Kolkata, five and a half hours ahead of UTC: late on the 23rd in UTC is the 24th for her."""
    for moment, expected in (
        (datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc), DAY),
        (datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc), TOMORROW.isoformat()),
    ):
        monkeypatch.setattr(ledger, "utc_now", lambda moment=moment: moment)
        data = ok(world.client.get("/v1/matches"))
        assert data["local_date"] == expected
    assert [m["decision_id"] for m in data["matches"]] == [str(world.tomorrow)]


def test_nobody_sees_anyone_elses_matches(world):
    world.sign_in(world.bob)
    data = ok(world.client.get("/v1/matches", params={"date": DAY}))
    assert [m["decision_id"] for m in data["matches"]] == [str(world.bobs)]
    assert data["run"]["id"] == str(world.bob_run)


def test_the_current_save_or_dismissal_is_on_the_match(world, engine):
    with engine.begin() as conn:
        for job, kind, reason in (
            (world.data.id, "save", None),
            (world.platform.id, "save", None),
            (world.platform.id, "dismiss", "location"),
        ):
            conn.execute(
                text(
                    """INSERT INTO feedback_events (user_id, job_id, kind, dismiss_reason, created_at)
                       VALUES (:u, :job, CAST(:kind AS feedback_kind), CAST(:reason AS dismiss_reason), clock_timestamp())"""
                ),
                {"u": world.ada, "job": job, "kind": kind, "reason": reason},
            )
    first, second = ok(world.client.get("/v1/matches", params={"date": DAY}))["matches"]
    assert first["feedback"] == {"is_saved": True, "is_dismissed": False}
    assert second["feedback"] == {"is_saved": False, "is_dismissed": True}  # the latest event decides


def test_the_five_a_day_cap_is_the_most_the_list_can_hold(world, engine):
    with engine.begin() as conn:
        for rank in range(3, 6):
            job = make_job(conn, title=f"Job {rank}", description="Python.", requirements=[("Python.", "required")])
            make_shown_decision(conn, world.run, world.ada, job, [Verdict("no_evidence")], rank_position=rank)
    matches = ok(world.client.get("/v1/matches", params={"date": DAY}))["matches"]
    assert [m["rank_position"] for m in matches] == [1, 2, 3, 4, 5]


def test_a_bad_date_is_a_validation_error(world):
    error = refused(world.client.get("/v1/matches", params={"date": "yesterday"}), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "date"


def test_the_match_routes_need_a_signed_in_person(api_client):
    for path in ("/v1/matches", f"/v1/matches/{uuid4()}"):
        refused(api_client.get(path), 401, "unauthenticated")


# ---- GET /v1/matches/{decision_id} -------------------------------------------------------------------------------


def test_one_match_adds_the_posting_text_the_constraint_checks_and_the_feedback(world, engine):
    with engine.begin() as conn:
        service.set_constraint(
            conn, world.ada, "salary_floor", {"salary_floor": {"amount": 3_000_000, "currency": "INR"}}, "30 lakh a year"
        )
        service.set_constraint(conn, world.ada, "employment_type", {"employment_types": ["full_time"]}, "Full time")
        service.set_constraint(conn, world.ada, "excluded_company", {"company_names": ["Hooli Demo"]}, "Never Hooli")
    match = ok(world.client.get(f"/v1/matches/{world.first}"))

    assert match["decision_id"] == str(world.first) and match["description"] == DESCRIPTION
    python = match["evidence"][0]["requirement"]["job_citation"]
    assert DESCRIPTION[python["span"]["start"] : python["span"]["end"]] == python["quote"] == "Python and SQL"
    cited = match["evidence"][0]["record_citations"][0]
    assert RESUME_TEXT[cited["source"]["span"]["start"] : cited["source"]["span"]["end"]] == cited["quote"]
    assert match["feedback"] == {"is_saved": False, "is_dismissed": False}
    checks = {c["kind"]: c for c in match["constraint_checks"]}
    assert set(checks) == {"location", "remote_policy", "salary_floor", "employment_type", "excluded_company"}
    assert checks["salary_floor"]["state"] == "not_stated"  # the decision carries salary_not_stated
    assert checks["location"] == {"kind": "location", "statement": "Only jobs in India.", "state": "satisfied"}
    assert checks["remote_policy"]["state"] == "satisfied" and checks["excluded_company"]["state"] == "satisfied"
    assert checks["employment_type"]["statement"] == "Full time"


@pytest.mark.parametrize(
    ("flag", "kinds"),
    [
        ("location_not_stated", {"location", "work_authorization"}),
        ("arrangement_not_stated", {"remote_policy"}),
        ("arrangement_ambiguous", {"remote_policy"}),
        ("salary_not_stated", {"salary_floor"}),
        ("salary_currency_differs", {"salary_floor"}),
        ("employment_type_not_stated", {"employment_type"}),
        ("seniority_not_stated", {"seniority"}),
    ],
)
def test_each_unverified_flag_marks_the_constraints_whose_field_the_posting_leaves_unstated(world, engine, flag, kinds):
    with engine.begin() as conn:
        for kind, fields in (
            ("work_authorization", {"country_codes": ["IN"]}),
            ("salary_floor", {"salary_floor": {"amount": 1_000_000, "currency": "INR"}}),
            ("employment_type", {"employment_types": ["full_time"]}),
            ("seniority", {"seniorities": ["senior"]}),
        ):
            service.set_constraint(conn, world.ada, kind, fields, f"Invented {kind}.")
        job = make_job(conn, title="Flagged", description="Python.", requirements=[("Python.", "required")])
        decision = make_shown_decision(
            conn, world.run, world.ada, job, [Verdict("no_evidence")], rank_position=3, unverified=(flag,)
        )
    checks = ok(world.client.get(f"/v1/matches/{decision}"))["constraint_checks"]
    assert {c["kind"] for c in checks if c["state"] == "not_stated"} == kinds
    assert {c["kind"] for c in checks if c["state"] == "satisfied"} == {
        "location",
        "remote_policy",
        "work_authorization",
        "salary_floor",
        "employment_type",
        "seniority",
    } - kinds


def test_a_withheld_decision_a_filtered_one_someone_elses_and_an_unknown_id_are_all_404(world, engine):
    with engine.begin() as conn:
        filtered = make_decision(
            conn,
            world.run,
            world.ada,
            make_job(conn, title="Violator", description="Python.", requirements=[("Python.", "required")]),
            outcome="filtered",
            reasons=("hard_constraint",),
            violated_kinds=("location",),
        )
    for decision in (world.held, filtered, world.bobs, uuid4()):
        refused(world.client.get(f"/v1/matches/{decision}"), 404, "not_found")
    refused(world.client.get("/v1/matches/not-an-id"), 422, "validation_failed")


# ---- the contract ------------------------------------------------------------------------------------------------


def test_the_openapi_document_describes_the_match_routes_and_marks_posting_and_resume_text_untrusted(api_client):
    spec = api_client.get("/openapi.json").json()
    assert "get" in spec["paths"]["/v1/matches"] and "get" in spec["paths"]["/v1/matches/{decision_id}"]
    schemas = spec["components"]["schemas"]
    marked = {
        (name, field)
        for name, schema in schemas.items()
        for field, prop in schema.get("properties", {}).items()
        if prop.get("x-untrusted-text") is True
    }
    assert {
        ("JobCitation", "quote"),
        ("RequirementRef", "text"),
        ("RecordCitation", "quote"),
        ("MatchDetail", "description"),
    } <= marked
    assert set(schemas["MatchDetail"]["properties"]) == set(schemas["MatchView"]["properties"]) | {
        "description",
        "constraint_checks",
    }
