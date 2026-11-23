"""MAT-06, MAT-05: GET /v1/decisions and /v1/decisions/{decision_id}, the decision ledger and "why not" (plan 04-04).

Rows are invented and built by hand with the 04-01 factories (the run pipeline of plan 04-03 is not used); the API reads
them as the real app_api role, so row-level security is in force. A second person has rows of her own throughout.
"""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.platform.sessions import Principal, current_user
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

DAY = LOCAL_DATE.isoformat()
YESTERDAY = LOCAL_DATE - timedelta(days=1)
SALARY = {"salary_floor": {"amount": 3_000_000, "currency": "INR"}}


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


def stamp(conn, moments: dict[UUID, str]) -> None:
    """Give ledger rows a creation time. The ledger is append-only; maintenance mode (what the retention sweep uses)
    lets one transaction of the test through."""
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    for decision, moment in moments.items():
        conn.execute(
            text("UPDATE match_decisions SET created_at = CAST(:at AS timestamptz) WHERE id = :id"),
            {"at": moment, "id": decision},
        )
    conn.execute(text("SELECT set_config('app.maintenance', 'off', true)"))


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


def seed(engine) -> SimpleNamespace:
    """Ada: a record with a salary floor, an excluded company and two other constraints; a nightly run today and one the
    day before, with a shown, a withheld-and-scored, a withheld-not-scored and a filtered decision between them."""
    w = SimpleNamespace()
    with engine.begin() as conn:
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
            company_name="Brightline Health",
            salary_min=1_800_000,
            salary_max=2_400_000,
            salary_currency="INR",
        )
        w.unscored = make_job(
            conn, title="Analyst", description="Analyse data.", requirements=[("Analyse data", "required")]
        )
        w.run = make_run(conn, w.ada, retrieved_count=30, filtered_count=388)
        w.yesterday_run = make_run(conn, w.ada, local_date=YESTERDAY, evidence_prompt_version=None)
        w.shown = make_shown_decision(
            conn, w.run, w.ada, w.good, [Verdict("met", w.record.fact_ids[1:2])], rank_position=1, summary="A fit."
        )
        w.gapped = make_decision(
            conn, w.run, w.ada, w.managing, outcome="withheld", reasons=("required_not_met",), retrieval_rank=2
        )
        add_evidence(conn, w.gapped, w.ada, w.managing, [Verdict("no_evidence"), Verdict("no_evidence")])
        w.filtered = make_decision(
            conn,
            w.run,
            w.ada,
            w.cheap,
            outcome="filtered",
            reasons=("hard_constraint",),
            violated_kinds=("salary_floor",),
        )
        w.old = make_decision(
            conn, w.yesterday_run, w.ada, w.unscored, outcome="withheld", reasons=("not_scored",), retrieval_rank=1
        )
        bob_run = make_run(conn, w.bob)
        w.bobs = make_decision(
            conn, bob_run, w.bob, w.managing, outcome="withheld", reasons=("not_scored",), retrieval_rank=1
        )
        stamp(conn, {w.old: "2026-11-22T06:00:00Z", w.shown: "2026-11-23T06:00:00Z", w.gapped: "2026-11-23T06:01:00Z"})
        stamp(conn, {w.filtered: "2026-11-23T06:02:00Z"})
        # Her constraints are older than the decisions above: a constraint is named as the reason for a decision only if
        # it already existed when the decision was made, and the stamps put the decisions in the past.
        conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        conn.execute(
            text("UPDATE constraints SET created_at = CAST(:at AS timestamptz) WHERE user_id = :u"),
            {"at": "2026-11-20T00:00:00Z", "u": w.ada},
        )
        conn.execute(text("SELECT set_config('app.maintenance', 'off', true)"))
    return w


@pytest.fixture
def world(api_client, engine) -> SimpleNamespace:
    w = seed(engine)
    w.client = api_client
    w.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    w.sign_in(w.ada)
    return w


def ids(rows) -> list[str]:
    return [row["decision_id"] for row in rows]


def counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("match_decisions", "match_runs", "match_evidence", "llm_calls", "queue_jobs")
        }


# ---- GET /v1/decisions --------------------------------------------------------------------------------------------


def test_the_ledger_lists_every_outcome_of_the_person_newest_first(world):
    rows = ok(world.client.get("/v1/decisions"))
    assert ids(rows) == [str(world.filtered), str(world.gapped), str(world.shown), str(world.old)]
    assert [r["local_date"] for r in rows] == [DAY, DAY, DAY, YESTERDAY.isoformat()]  # nothing of Bob's is here


def test_a_filtered_row_names_the_constraint_that_removed_it_and_a_scored_withheld_row_its_gaps(world):
    rows = {r["decision_id"]: r for r in ok(world.client.get("/v1/decisions", params={"outcome": "withheld,filtered"}))}
    assert set(rows) == {str(world.gapped), str(world.filtered), str(world.old)}

    filtered = rows[str(world.filtered)]
    assert (filtered["outcome"], filtered["reason_codes"]) == ("filtered", ["hard_constraint"])
    assert filtered["constraint"] == {
        "id": world.floor,
        "kind": "salary_floor",
        "statement": "Nothing below 30 lakh a year.",
    }
    assert filtered["gaps"] == [] and filtered["evaluated"] is None
    verified = filtered["job"].pop("last_verified_at")
    assert verified.endswith("Z")
    assert filtered["job"] == {
        "id": world.cheap.id,
        "title": "Backend engineer II",
        "company_name": "Brightline Health",
        "pay": {"min": 1_800_000, "max": 2_400_000, "currency": "INR"},
        "status": "open",
    }

    gapped = rows[str(world.gapped)]
    assert (gapped["outcome"], gapped["reason_codes"], gapped["constraint"]) == ("withheld", ["required_not_met"], None)
    assert gapped["gaps"] == [  # the required item with no evidence; the preferred one is not a gap
        {"requirement": "Three years managing engineering teams", "importance": "required", "verdict": "no_evidence"}
    ]
    assert gapped["job"]["pay"] is None

    unscored = rows[str(world.old)]
    assert (unscored["reason_codes"], unscored["gaps"], unscored["constraint"]) == (["not_scored"], [], None)


def test_a_constraint_the_person_has_since_removed_leaves_the_kind_and_no_statement(world, engine):
    with engine.begin() as conn:
        service.remove_constraint(conn, world.ada, UUID(world.floor))
    rows = ok(world.client.get("/v1/decisions", params={"outcome": "filtered"}))
    assert [r["constraint"] for r in rows] == [{"id": None, "kind": "salary_floor", "statement": None}]


def test_a_constraint_replaced_after_the_decision_is_not_named_as_the_one_that_filtered_the_job(world, engine):
    """The job was filtered under the old pay floor, and the new floor may let it through, so the ledger cannot say the
    new sentence removed it. A replaced constraint reads like a removed one: the kind stays, the sentence does not."""
    with engine.begin() as conn:
        service.set_constraint(
            conn,
            world.ada,
            "salary_floor",
            {"salary_floor": {"amount": 1_500_000, "currency": "INR"}},
            "Nothing below 15 lakh a year.",
        )
    nobody = {"id": None, "kind": "salary_floor", "statement": None}
    rows = ok(world.client.get("/v1/decisions", params={"outcome": "filtered"}))
    assert [r["constraint"] for r in rows] == [nobody]
    assert ok(world.client.get(f"/v1/decisions/{world.filtered}"))["constraint"] == nobody
    (row,) = ok(world.client.get("/v1/decisions", params={"job_id": world.cheap.id}))
    assert row["constraint"] == nobody  # the stored decision for the job, not a fresh check


def test_the_filters_narrow_the_list_by_outcome_reason_and_date(world):
    get = world.client.get
    assert ids(ok(get("/v1/decisions", params={"outcome": "shown"}))) == [str(world.shown)]
    assert set(ids(ok(get("/v1/decisions", params={"outcome": "filtered,shown"})))) == {
        str(world.shown),
        str(world.filtered),
    }
    assert ids(ok(get("/v1/decisions", params={"reason": "not_scored"}))) == [str(world.old)]
    assert ids(ok(get("/v1/decisions", params={"reason": "required_not_met"}))) == [str(world.gapped)]
    assert ids(ok(get("/v1/decisions", params={"to": YESTERDAY.isoformat()}))) == [str(world.old)]
    assert set(ids(ok(get("/v1/decisions", params={"from": DAY})))) == {
        str(world.shown),
        str(world.gapped),
        str(world.filtered),
    }
    both = ok(
        get("/v1/decisions", params={"from": DAY, "to": DAY, "outcome": "withheld", "reason": "required_not_met"})
    )
    assert ids(both) == [str(world.gapped)]
    assert ok(get("/v1/decisions", params={"from": "2030-01-01"})) == []


def test_pages_follow_the_cursor_without_a_gap_or_a_repeat(world):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        response = world.client.get("/v1/decisions", params=params)
        seen += ids(ok(response))
        pages += 1
        assert response.json()["meta"]["limit"] == 1
        cursor = response.json()["meta"]["next_cursor"]
        if cursor is None:
            break
    assert pages == 4 and len(seen) == len(set(seen)) == 4
    assert set(seen) == {str(world.shown), str(world.gapped), str(world.filtered), str(world.old)}
    assert seen[-1] == str(world.old)  # the oldest local date comes last


def test_an_unknown_filter_value_or_a_cursor_nobody_issued_is_a_400_and_a_bad_limit_a_422(world):
    get = world.client.get
    refused(get("/v1/decisions", params={"outcome": "shown,bogus"}), 400, "invalid_request")
    refused(get("/v1/decisions", params={"reason": "bogus"}), 400, "invalid_request")
    refused(get("/v1/decisions", params={"cursor": "not-a-cursor"}), 400, "invalid_request")
    for parts in (["2026-11-23", "2026-11-23T06:00:00+00:00", 5], ["2026-11-23"], {"a": "b"}, ["x", "y", "z"]):
        forged = base64.urlsafe_b64encode(json.dumps(parts).encode()).decode()  # decodes, but this API never issued it
        refused(get("/v1/decisions", params={"cursor": forged}), 400, "invalid_request")
    refused(get("/v1/decisions", params={"limit": 0}), 422, "validation_failed")
    refused(get("/v1/decisions", params={"from": "soon"}), 422, "validation_failed")


def test_nobody_sees_anyone_elses_ledger(world):
    world.sign_in(world.bob)
    assert ids(ok(world.client.get("/v1/decisions"))) == [str(world.bobs)]


# ---- GET /v1/decisions?job_id= --------------------------------------------------------------------------------------


def test_job_id_returns_the_newest_stored_decision_for_that_job(world, engine):
    with engine.begin() as conn:
        again = make_decision(
            conn,
            make_run(conn, world.ada, local_date=LOCAL_DATE + timedelta(days=1)),
            world.ada,
            world.managing,
            outcome="withheld",
            reasons=("weak_fit",),
            retrieval_rank=4,
        )
    rows = ok(world.client.get("/v1/decisions", params={"job_id": world.managing.id}))
    assert ids(rows) == [str(again)] and rows[0]["reason_codes"] == ["weak_fit"] and rows[0]["evaluated"] is None
    assert ids(ok(world.client.get("/v1/decisions", params={"job_id": world.good.id}))) == [str(world.shown)]


def test_a_job_never_decided_that_the_constraints_exclude_comes_back_filtered_on_request_and_stores_nothing(
    world, engine
):
    with engine.begin() as conn:
        excluded = make_job(
            conn,
            title="Platform engineer",
            description="Platform work.",
            requirements=[("Platform work", "required")],
            company_name="Ashgrove Capital, Inc.",
            country="IN",
        )
    before = counts(engine)
    (row,) = ok(world.client.get("/v1/decisions", params={"job_id": excluded.id}))

    assert row["decision_id"] is None and row["evaluated"] == "on_request"
    assert (row["outcome"], row["reason_codes"], row["gaps"]) == ("filtered", ["hard_constraint"], [])
    assert row["constraint"] == {"id": world.never, "kind": "excluded_company", "statement": "Never Ashgrove Capital."}
    assert (row["job"]["id"], row["job"]["company_name"]) == (excluded.id, "Ashgrove Capital, Inc.")
    assert row["local_date"] is not None
    assert counts(engine) == before  # no decision, no run, no model call


def test_a_job_that_passes_every_constraint_and_was_never_scored_is_not_evaluated(world, engine):
    with engine.begin() as conn:
        fine = make_job(
            conn, title="Fine", description="Fine work.", requirements=[("Fine work", "required")], country="IN"
        )
    before = counts(engine)
    (row,) = ok(world.client.get("/v1/decisions", params={"job_id": fine.id}))
    assert (row["decision_id"], row["outcome"], row["reason_codes"], row["constraint"]) == (
        None,
        "not_evaluated",
        [],
        None,
    )
    assert row["evaluated"] == "on_request" and counts(engine) == before


def test_an_unknown_job_is_a_404(world):
    refused(world.client.get("/v1/decisions", params={"job_id": 987654321}), 404, "not_found")


# ---- GET /v1/decisions/{decision_id} --------------------------------------------------------------------------------


def test_one_decision_is_its_whole_row_with_its_evidence(world):
    held = ok(world.client.get(f"/v1/decisions/{world.gapped}"))
    assert held["decision_id"] == str(world.gapped) and held["outcome"] == "withheld"
    assert (held["violated_kinds"], held["unverified"], held["rank_position"]) == ([], [], None)
    assert (held["summary"], held["change_note"]) == (None, None)
    assert [(e["requirement"]["text"], e["verdict"]) for e in held["evidence"]] == [
        ("Three years managing engineering teams", "no_evidence"),
        ("Python", "no_evidence"),
    ]
    assert held["gaps"] and held["job"]["id"] == world.managing.id

    shown = ok(world.client.get(f"/v1/decisions/{world.shown}"))
    assert (shown["outcome"], shown["rank_position"], shown["summary"]) == ("shown", 1, "A fit.")
    assert shown["evidence"][0]["verdict"] == "met" and shown["evidence"][0]["record_citations"][0]["is_current"]

    filtered = ok(world.client.get(f"/v1/decisions/{world.filtered}"))
    assert (filtered["violated_kinds"], filtered["evidence"], filtered["constraint"]["kind"]) == (
        ["salary_floor"],
        [],
        "salary_floor",
    )


def test_another_persons_decision_and_an_unknown_one_are_404(world):
    refused(world.client.get(f"/v1/decisions/{world.bobs}"), 404, "not_found")
    refused(world.client.get(f"/v1/decisions/{uuid4()}"), 404, "not_found")
    refused(world.client.get("/v1/decisions/nope"), 422, "validation_failed")


def test_the_decision_routes_need_a_signed_in_person(api_client):
    for path in ("/v1/decisions", f"/v1/decisions/{uuid4()}"):
        refused(api_client.get(path), 401, "unauthenticated")


def test_the_openapi_document_describes_the_decision_routes_and_marks_the_requirement_text_untrusted(api_client):
    spec = api_client.get("/openapi.json").json()
    assert "get" in spec["paths"]["/v1/decisions"] and "get" in spec["paths"]["/v1/decisions/{decision_id}"]
    schemas = spec["components"]["schemas"]
    assert schemas["Gap"]["properties"]["requirement"]["x-untrusted-text"] is True
    assert set(schemas["DecisionDetail"]["properties"]) >= set(schemas["DecisionView"]["properties"])
    assert "not_evaluated" in str(schemas["DecisionView"]["properties"]["outcome"])
