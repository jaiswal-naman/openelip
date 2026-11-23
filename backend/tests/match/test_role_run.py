"""HIR-03, HIR-04 and HIR-07 (plan 06-05, task 1): the role run, an employer's role against pooled candidates.

Candidates and the role are invented (tests/hiring/factories.py, whose requirements the recordings in
fixtures/role_run answer). The model is the real gateway on the fake provider. A run opens its own connections (the
match role and the worker), so what a test builds is committed and the tables are emptied around each test.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.match import api, evidence, ledger, queries
from app.platform import db
from app.platform.llm import ProviderUnavailable
from tests.hiring import factories as hiring
from tests.hiring import world as hiring_world
from tests.match import factories
from tests.match.fixtures.run import world

RECORDINGS = world.Path(__file__).parent / "fixtures" / "role_run"
EMPTY = text(
    "TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls, eval_runs, organizations RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    def empty():
        with engine.begin() as c:
            c.execute(EMPTY)

    empty()
    world.no_embedder(monkeypatch)
    yield
    empty()


@pytest.fixture
def provider(monkeypatch):
    """provider("all_met.json", LLM_PROVIDER=...): the gateway on the fake provider answering from fixtures/role_run."""

    def use(recording="all_met.json", **env):
        calls = world.use_provider(monkeypatch, recording, **env)
        monkeypatch.setattr(world.fake, "_source", RECORDINGS / recording)
        return calls

    return use


def make_role(engine, **fields):
    with engine.begin() as c:
        org = hiring.make_org(c)
        return hiring.make_open_role(c, org, **fields)


def make_candidate(engine, **fields):
    with engine.begin() as c:
        return hiring.make_candidate(c, **fields)


def role_decisions(engine, role, where="true"):
    return world.fetch(
        engine,
        f"""SELECT d.user_id, d.outcome::text AS outcome, d.reason_codes::text[] AS reasons, d.summary, d.local_date,
                   d.rank_position, d.match_run_id,
                   (SELECT count(*) FROM match_evidence e WHERE e.match_decision_id = d.id) AS evidence
              FROM match_decisions d WHERE d.job_id = :job AND {where} ORDER BY d.outcome, d.user_id""",
        job=role.job_id,
    )


def today_of(conn, user):
    return conn.execute(
        text("SELECT (now() AT TIME ZONE u.timezone)::date FROM users u WHERE id = :u"), {"u": user}
    ).scalar_one()


def run_row(engine, run_id):
    (row,) = world.fetch(engine, "SELECT * FROM match_runs WHERE id = :r", r=run_id)
    return row


# --- the filter first, then the verdicts ------------------------------------------------------------------------------


def test_blocked_and_constrained_candidates_are_counted_not_stored_and_never_scored(engine, provider):
    calls = provider()
    role = make_role(engine, country="IN", arrangements=("remote",))
    blocked, abroad, ada, bob = (make_candidate(engine) for _ in range(4))
    with engine.begin() as c:
        hiring_world.exclude_company(c, blocked.id, role.company)
        world.drop_constraints(c, abroad.id)
        world.add_constraint(c, abroad.id, "location", country_codes=["DE"])

    result = api.run_role(role.id, role.job_id, [blocked.id, abroad.id, ada.id, bob.id])

    assert (result.filtered, result.scored, result.withheld) == (2, 2, 0)
    assert sorted(s.user_id for s in result.selected) == sorted([ada.id, bob.id])
    stored = role_decisions(engine, role)
    assert sorted(d.user_id for d in stored) == sorted([ada.id, bob.id])  # a filtered candidate has no row at all
    assert all(d.outcome == "introduced" and d.reasons == ["selected_for_role"] and d.evidence == 3 for d in stored)
    assert all(d.summary.startswith("Meets 2 of 2 required items.") and d.rank_position is None for d in stored)
    assert [call[0] for call in calls] == ["match.evidence.score"] * 2  # nothing was sent for the two filtered


def test_a_selection_carries_one_evidence_row_per_requirement_with_the_cited_facts(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id])

    (selection,) = result.selected
    assert [(r.requirement, r.importance, r.verdict) for r in selection.rows] == [
        ("Python is required.", "required", "met"),
        ("SQL is required.", "required", "met"),
        ("Experience with Airflow is a plus.", "preferred", "no_evidence"),
    ]
    assert [r.fact_ids for r in selection.rows] == [(ada.record.fact_ids[1],), (ada.record.fact_ids[1],), ()]
    assert selection.summary.startswith("Meets 2 of 2 required items.")


def test_a_required_item_not_met_is_withheld_with_its_evidence(engine, provider):
    provider("required_not_met.json")
    role = make_role(engine)
    ada = make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id])

    assert (result.selected, result.withheld, result.scored) == ([], 1, 1)
    (d,) = role_decisions(engine, role)
    assert (d.outcome, d.reasons, d.evidence) == ("withheld", ["required_not_met"], 3)


def test_a_weak_fit_verdict_set_is_withheld_weak_fit(engine, provider):
    provider("weak_fit.json")
    role = make_role(engine)
    ada = make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id])

    assert (result.selected, result.withheld) == ([], 1)
    (d,) = role_decisions(engine, role)
    assert (d.outcome, d.reasons) == ("withheld", ["weak_fit"])


def test_a_set_the_validator_refuses_twice_is_withheld_citation_unresolved_with_no_evidence(engine, provider):
    calls = provider("bad_twice.json")
    role = make_role(engine)
    ada = make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id])

    assert (result.selected, result.withheld, result.scored) == ([], 1, 1)
    (d,) = role_decisions(engine, role)
    assert (d.outcome, d.reasons, d.evidence) == ("withheld", ["citation_unresolved"], 0)
    assert len(calls) == 2  # one fresh call after the first refusal


def test_an_invalid_answer_twice_is_withheld_not_scored(engine, provider):
    provider("invalid_schema_twice.json")
    role = make_role(engine)
    ada = make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id])

    assert (result.selected, result.withheld, result.scored) == ([], 1, 0)
    (d,) = role_decisions(engine, role)
    assert (d.outcome, d.reasons, d.evidence) == ("withheld", ["not_scored"], 0)


def test_with_no_provider_every_unfiltered_candidate_is_withheld_not_scored(engine, provider):
    calls = provider(LLM_PROVIDER="anthropic")  # no key: AI is off
    role = make_role(engine)
    ada, bob = make_candidate(engine), make_candidate(engine)

    result = api.run_role(role.id, role.job_id, [ada.id, bob.id])

    assert (result.selected, result.withheld, result.scored, result.filtered) == ([], 2, 0, 0)
    assert [(d.outcome, d.reasons, d.evidence) for d in role_decisions(engine, role)] == [
        ("withheld", ["not_scored"], 0)
    ] * 2
    run = run_row(engine, result.run_id)
    assert (run.status, run.evidence_prompt_version) == ("completed", None)
    assert calls == []


def test_provider_unavailable_marks_the_run_failed_and_propagates(engine, provider, monkeypatch):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)

    def down(request, *, principal):
        raise ProviderUnavailable()

    monkeypatch.setattr(evidence, "score", down)
    with pytest.raises(ProviderUnavailable):
        api.run_role(role.id, role.job_id, [ada.id])

    (run,) = world.fetch(
        engine, "SELECT status::text AS status, error_code FROM match_runs WHERE role_id = :r", r=role.id
    )
    assert (run.status, run.error_code) == ("failed", "provider_unavailable")
    assert role_decisions(engine, role) == []


# --- the limit, the order, the run row ---


def test_score_limit_scores_the_first_and_withholds_the_rest_and_the_next_run_starts_with_those(engine, provider):
    calls = provider()
    role = make_role(engine)
    people = [make_candidate(engine) for _ in range(3)]
    ids = [p.id for p in people]

    first = api.run_role(role.id, role.job_id, ids, score_limit=2)

    assert (first.scored, len(first.selected), first.withheld) == (2, 2, 1)
    left_out = next(d for d in role_decisions(engine, role) if d.outcome == "withheld")
    assert left_out.reasons == ["not_scored"]
    assert len(calls) == 2

    second = api.run_role(role.id, role.job_id, ids, score_limit=1)

    assert [s.user_id for s in second.selected] == [left_out.user_id]  # the one never scored for this role goes first


def test_the_run_row_is_a_role_run_with_the_utc_date_and_counts_and_decisions_use_the_candidates_own_date(
    engine, provider
):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET timezone = 'Pacific/Kiritimati' WHERE id = :u"), {"u": ada.id})

    result = api.run_role(role.id, role.job_id, [ada.id])

    run = run_row(engine, result.run_id)
    assert (run.trigger, run.role_id, run.user_id, run.status) == ("role", role.id, None, "completed")
    assert run.local_date == run.started_at.date()
    assert (run.retrieved_count, run.filtered_count, run.shown_count) == (1, 0, 0)
    assert run.evidence_prompt_version == evidence.PROMPT_VERSION and run.finished_at is not None
    (d,) = role_decisions(engine, role)
    assert d.local_date in (run.local_date, run.local_date + timedelta(days=1))  # UTC+14 is the day ahead or the same


def test_a_candidate_with_five_shown_matches_today_is_still_introduced(engine, provider):
    provider()
    role = make_role(engine)
    ada = make_candidate(engine)
    with engine.begin() as c:
        run = factories.make_run(c, ada.id, local_date=today_of(c, ada.id))
        for n in range(1, 6):
            job = factories.make_job(
                c, title=f"Shown {n}", description="Python is used here.", requirements=(("Python", "required"),)
            )
            verdicts = [factories.Verdict("met", (ada.record.fact_ids[1],))]
            factories.make_shown_decision(c, run, ada.id, job, verdicts, rank_position=n)

    result = api.run_role(role.id, role.job_id, [ada.id])

    assert [s.user_id for s in result.selected] == [ada.id]


# --- reads ------------------------------------------------------------------------------------------------------------


def test_role_selections_and_latest_role_run_read_the_same_back_from_the_database(engine, provider):
    provider()
    role = make_role(engine)
    ada, bob = make_candidate(engine), make_candidate(engine)

    first = api.run_role(role.id, role.job_id, [ada.id])
    second = api.run_role(role.id, role.job_id, [ada.id, bob.id])

    with db.transaction(role="worker") as conn:
        assert {s.decision_id: s for s in api.role_selections(conn, second.run_id)} == {
            s.decision_id: s for s in second.selected
        }
        assert api.role_selections(conn, first.run_id) == first.selected
        latest = api.latest_role_run(conn, role.id)
        assert api.latest_role_run(conn, uuid4()) is None
    assert latest is not None and latest.run_id == second.run_id and latest.status == "completed"


# --- a role decision never reaches the candidate's own views ---


def test_role_decisions_stay_out_of_the_candidates_matches_ledger_and_brief(engine, provider):
    provider(LLM_PROVIDER="anthropic")  # not scored: the withheld decisions a brief might list as "not yet assessed"
    role = make_role(engine)
    ada = make_candidate(engine)
    api.run_role(role.id, role.job_id, [ada.id])
    provider("all_met.json")
    result = api.run_role(role.id, role.job_id, [ada.id])  # and an introduced one, too
    assert len(result.selected) == 1
    stored = role_decisions(engine, role)
    assert {d.outcome for d in stored} == {"withheld", "introduced"}
    day = stored[0].local_date
    with engine.begin() as c:  # the decisions are there for the worker's reads, so the exclusion is what hides them
        assert (
            c.execute(text("SELECT count(*) FROM match_decisions WHERE user_id = :u"), {"u": ada.id}).scalar_one() == 2
        )

    with db.transaction(user_id=ada.id, role="api") as conn:
        assert ledger.shown_matches(conn, ada.id, day) == []
        page, _ = ledger.decisions_page(conn, ada.id)
        assert page == []
        assert ledger.decision_by_id(conn, ada.id, result.selected[0].decision_id) is None
        assert ledger.match_by_id(conn, ada.id, result.selected[0].decision_id) is None
        assert queries.brief_matches(conn, ada.id, day) == []
    with db.transaction(user_id=ada.id, role="worker") as conn:  # the worker reads every row: the exclusion alone
        assert queries.brief_matches(conn, ada.id, day) == []
        assert ledger.decisions_page(conn, ada.id)[0] == []
        assert ledger.decision_by_id(conn, ada.id, result.selected[0].decision_id) is None
        assert ledger.run_summary(conn, ada.id, day) is None


def test_the_read_time_constraint_recheck_still_holds_beside_the_role_exclusion(engine, provider):
    """Phase 4's fix B2: a shown match whose job the person now excludes is never listed. Adding the role exclusion must
    not replace that re-check."""
    provider()
    ada = make_candidate(engine)
    with engine.begin() as c:
        job = factories.make_job(
            c,
            title="Role A",
            description="Python is used here.",
            requirements=(("Python", "required"),),
            country="IN",
            arrangements=("remote",),
        )
        today = today_of(c, ada.id)
        run = factories.make_run(c, ada.id, local_date=today)
        factories.make_shown_decision(c, run, ada.id, job, [factories.Verdict("met", (ada.record.fact_ids[1],))])
    with db.transaction(user_id=ada.id, role="api") as conn:
        assert len(ledger.shown_matches(conn, ada.id, today)) == 1
    with engine.begin() as c:
        hiring_world.exclude_company(c, ada.id, "Initech Demo")
    with db.transaction(user_id=ada.id, role="api") as conn:
        assert ledger.shown_matches(conn, ada.id, today) == []
        assert queries.brief_matches(conn, ada.id, today) == []
