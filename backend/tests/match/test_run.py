"""MAT-01 to MAT-03, MAT-05 and MAT-06 (plan 04-03, task 2): the match run, from retrieval to the decision ledger.

The candidate and the postings are invented (fixtures/run/README.md). The model is the real gateway on the fake
provider, answering from recordings, so a run is exercised end to end: consent, the match role's reads, scoring, the
validator and its one retry, the checker, the ranker under the daily cap, and the writes that the database trigger
checks again. Everything a run reads is committed, so the tables are emptied around each test.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from app.match import api, evidence, ranking, service
from app.match.validator import Verdict, VerdictSet
from app.platform import config
from app.platform.llm import ProviderUnavailable, registry
from tests.match import factories
from tests.match.fixtures.run import world

LOCAL_DATE = world.LOCAL_DATE


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    world.empty_tables(engine)
    world.no_embedder(monkeypatch)
    yield
    world.empty_tables(engine)


@pytest.fixture
def provider(monkeypatch):
    """provider("all_met.json", LLM_PROVIDER=...): the gateway on the fake provider; returns the provider requests."""

    def use(recording="all_met.json", **env):
        return world.use_provider(monkeypatch, recording, **env)

    return use


def decisions(engine, user, where="true"):
    return world.fetch(
        engine,
        f"""SELECT d.job_id, j.title, d.outcome::text AS outcome, d.reason_codes::text[] AS reasons, d.rank_position,
                   d.violated_kinds::text[] AS violated, d.unverified::text[] AS unverified, d.summary, d.fit_score,
                   d.retrieval_rank, d.job_content_version, d.change_note,
                   (SELECT count(*) FROM match_evidence e WHERE e.match_decision_id = d.id) AS evidence
              FROM match_decisions d JOIN jobs j ON j.id = d.job_id
             WHERE d.user_id = :u AND {where} ORDER BY d.outcome, d.rank_position, d.job_id""",
        u=user,
    )


def run_row(engine, run_id):
    (row,) = world.fetch(engine, "SELECT * FROM match_runs WHERE id = :r", r=run_id)
    return row


# --- the happy path and the cap -----------------------------------------------------------------------------------


def test_a_run_shows_five_of_seven_with_evidence_and_records_why_not_for_the_rest(engine, provider):
    calls = provider()
    user = world.add_person(engine)
    jobs = [world.add_job(engine, f"Role {name}") for name in "ABCDEFG"]
    abroad = [world.add_job(engine, f"Role abroad {n}", country="DE") for n in range(3)]

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    shown = decisions(engine, user, "d.outcome = 'shown'")
    assert [d.job_id for d in shown] == [job.id for job in jobs[:5]]  # equal scores: the lower job id first
    assert [d.rank_position for d in shown] == [1, 2, 3, 4, 5]
    assert all(d.reasons == ["top_ranked"] and d.evidence == 2 for d in shown)
    assert all(d.summary.startswith("Meets 2 of 2 required items.") for d in shown)
    held = decisions(engine, user, "d.outcome = 'withheld'")
    assert [(d.job_id, d.reasons, d.evidence) for d in held] == [(job.id, ["below_daily_cap"], 2) for job in jobs[5:]]
    filtered = decisions(engine, user, "d.outcome = 'filtered'")
    assert sorted(d.job_id for d in filtered) == sorted(job.id for job in abroad)
    assert all(d.reasons == ["hard_constraint"] and d.violated == ["location"] and d.evidence == 0 for d in filtered)

    run = run_row(engine, result.run_id)
    assert (run.status, run.trigger, run.local_date, run.error_code) == ("completed", "onboarding", LOCAL_DATE, None)
    assert (run.ranker_version, run.evidence_prompt_version) == (ranking.RANKER_VERSION, evidence.PROMPT_VERSION)
    assert (run.retrieved_count, run.filtered_count, run.shown_count) == (7, 3, 5)
    assert run.finished_at is not None and run.record_version == 1
    assert result == service.RunResult(result.run_id, shown=5, withheld=2, filtered=3, checker="agreed")
    assert [call[0] for call in calls] == ["match.evidence.score"] * 7  # one provider request per scored job


def test_a_second_run_on_the_same_date_fills_only_the_open_slots(engine, provider):
    provider()
    user = world.add_person(engine)
    for name in "ABC":
        world.add_job(engine, f"Role {name}")
    first = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)
    for name in "DEFG":
        world.add_job(engine, f"Role {name}")

    second = service.run(user, trigger="on_demand", local_date=LOCAL_DATE)

    assert (first.shown, second.shown, second.withheld) == (3, 2, 2)
    shown = decisions(engine, user, "d.outcome = 'shown'")
    assert [d.rank_position for d in shown] == [1, 2, 3, 4, 5]  # the day's five, whatever started the run
    assert [d.title for d in shown] == ["Role A", "Role B", "Role C", "Role D", "Role E"]


def test_a_second_nightly_run_for_a_date_is_a_no_op_that_returns_the_first(engine, provider):
    calls = provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")
    first = service.run(user, trigger="nightly", local_date=LOCAL_DATE)
    spent, rows = len(calls), decisions(engine, user)

    again = service.run(user, trigger="nightly", local_date=LOCAL_DATE)

    assert again == first and first.shown == 1
    assert len(calls) == spent and decisions(engine, user) == rows
    assert len(world.fetch(engine, "SELECT 1 FROM match_runs WHERE user_id = :u", u=user)) == 1


def test_a_run_with_nothing_eligible_completes_with_zero_shown(engine, provider):
    calls = provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role abroad", country="DE")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld, result.filtered, result.checker) == (0, 0, 1, "agreed")
    run = run_row(engine, result.run_id)
    assert (run.status, run.error_code, run.evidence_prompt_version, run.retrieved_count) == (
        "completed",
        None,
        None,
        0,
    )
    assert calls == []


def test_only_the_twenty_best_ranked_violators_are_stored_and_every_one_is_counted(engine, provider):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")
    for n in range(25):
        world.add_job(engine, f"Role abroad {n:02d}", country="DE")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.filtered) == (1, 25)  # the count is of all of them
    assert len(decisions(engine, user, "d.outcome = 'filtered'")) == 20  # the rows are the best twenty
    assert run_row(engine, result.run_id).filtered_count == 25


def test_a_job_that_does_not_state_its_place_is_shown_with_the_field_flagged(engine, provider):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A", country=None)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert result.shown == 1
    (shown,) = decisions(engine, user, "d.outcome = 'shown'")
    assert shown.unverified == ["location_not_stated"]


# --- the verdicts: one retry, then withheld ------------------------------------------------------------------------


def test_a_rejected_verdict_set_gets_one_retry_and_a_valid_second_answer_is_shown(engine, provider):
    calls = provider("unknown_alias_then_valid.json")
    user = world.add_person(engine)
    world.add_job(engine, "Role A")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert result.shown == 1 and len(calls) == 2
    (shown,) = decisions(engine, user)
    assert (shown.outcome, shown.evidence) == ("shown", 2)


def test_two_rejected_sets_withhold_the_job_as_citation_unresolved_with_no_evidence(engine, provider):
    calls = provider("bad_twice.json")
    user = world.add_person(engine)
    world.add_job(engine, "Role A")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert result.shown == 0 and len(calls) == 2
    (withheld,) = decisions(engine, user)
    assert (withheld.outcome, withheld.reasons, withheld.evidence) == ("withheld", ["citation_unresolved"], 0)
    assert withheld.fit_score is None
    assert run_row(engine, result.run_id).evidence_prompt_version == evidence.PROMPT_VERSION  # the model did answer


def test_an_answer_that_never_fits_the_schema_withholds_the_job_as_not_scored(engine, provider):
    calls = provider("invalid_schema_twice.json")
    user = world.add_person(engine)
    world.add_job(engine, "Role A")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert result.shown == 0 and [call[3] for call in calls] == ["schema_invalid", "schema_invalid"]
    (withheld,) = decisions(engine, user)
    assert (withheld.outcome, withheld.reasons, withheld.evidence) == ("withheld", ["not_scored"], 0)


def scripted(monkeypatch, answer_for):
    """Replace evidence.score by a function of the request that answers with {alias: (verdict, [fact aliases])}."""

    def score(request, *, principal):
        answer = answer_for(request)
        return VerdictSet(verdicts=[Verdict(requirement=r, verdict=v, facts=f) for r, (v, f) in answer.items()])

    monkeypatch.setattr(evidence, "score", score)


def supersede_python_skill(engine, user):
    with engine.begin() as c:
        c.execute(
            text("UPDATE profile_facts SET superseded_at = now() WHERE user_id = :u AND kind = 'skill'"), {"u": user}
        )


def test_a_fact_superseded_between_scoring_and_writing_withholds_that_job_alone(engine, provider, monkeypatch):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")
    world.add_job(engine, "Role B", requirements=(("Python", "required"), ("Airflow", "preferred")))

    def answer_for(request):
        if any(r.text == "Airflow" for r in request.requirements.values()):
            supersede_python_skill(engine, user)  # the person retires the fact the model is about to be cited on
            return {"r1": ("met", ["f4"]), "r2": ("no_evidence", [])}  # f4 is the skill Python
        return {"r1": ("met", ["f2"]), "r2": ("met", ["f2"])}

    scripted(monkeypatch, answer_for)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (1, 1)
    by_title = {d.title: d for d in decisions(engine, user)}
    assert (by_title["Role A"].outcome, by_title["Role A"].evidence) == ("shown", 2)
    assert (by_title["Role B"].outcome, by_title["Role B"].reasons) == ("withheld", ["citation_unresolved"])
    assert by_title["Role B"].evidence == 0
    assert run_row(engine, result.run_id).status == "completed"


def test_a_shown_decision_with_an_evidence_row_missing_is_withheld_alone_and_the_run_commits(
    engine, provider, monkeypatch
):
    provider()
    user = world.add_person(engine)
    broken = world.add_job(engine, "Role A")  # the first pick, so it asks for position 1 first
    world.add_job(engine, "Role B")
    write = service._write_evidence

    def drop_the_last_row(conn, decision_id, user_id, rows):
        write(conn, decision_id, user_id, rows[:-1] if rows[0].requirement_id == broken.requirement_ids[0] else rows)

    monkeypatch.setattr(service, "_write_evidence", drop_the_last_row)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (1, 1)
    by_title = {d.title: d for d in decisions(engine, user)}
    assert (by_title["Role A"].outcome, by_title["Role A"].reasons) == ("withheld", ["citation_unresolved"])
    assert by_title["Role A"].evidence == 0  # its half-written evidence went with the savepoint
    assert (by_title["Role B"].outcome, by_title["Role B"].rank_position) == ("shown", 1)  # position 1 was left free


# --- the job moved while the run scored ----------------------------------------------------------------------------


def during_scoring(monkeypatch, before_answering):
    """Run `before_answering(request)` the first time the model is asked, then answer every requirement met by f2."""
    done = []

    def answer_for(request):
        if not done:
            done.append(True)
            before_answering(request)
        return {alias: ("met", ["f2"]) for alias in request.requirements}

    scripted(monkeypatch, answer_for)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("UPDATE jobs SET status = 'closed', closed_at = now() WHERE title = 'Role A'", "job_closed"),
        ("UPDATE jobs SET content_version = content_version + 1 WHERE title = 'Role A'", "not_scored"),
    ],
)
def test_a_job_that_closed_or_changed_while_the_run_scored_is_withheld_without_evidence(
    engine, provider, monkeypatch, change, reason
):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")
    world.add_job(engine, "Role B")

    def change_the_job(request):
        with engine.begin() as c:
            c.execute(text(change))

    during_scoring(monkeypatch, change_the_job)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (1, 1)
    by_title = {d.title: d for d in decisions(engine, user)}
    assert (by_title["Role A"].outcome, by_title["Role A"].reasons, by_title["Role A"].evidence) == (
        "withheld",
        [reason],
        0,
    )
    assert (by_title["Role B"].outcome, by_title["Role B"].rank_position) == ("shown", 1)


def test_a_job_shown_by_another_run_while_this_one_scored_is_not_shown_twice(engine, provider, monkeypatch):
    provider()
    user = world.add_person(engine)
    first = world.add_job(engine, "Role A")
    world.add_job(engine, "Role B")

    def another_run_shows_role_a(request):
        with engine.begin() as c:
            fact = c.execute(
                text("SELECT id FROM profile_facts WHERE user_id = :u AND kind = 'role'"), {"u": user}
            ).scalar_one()
            run = factories.make_run(c, user, trigger="on_demand", local_date=LOCAL_DATE)
            factories.make_shown_decision(c, run, user, first, [factories.Verdict("met", (fact,))] * 2, rank_position=1)

    during_scoring(monkeypatch, another_run_shows_role_a)

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (1, 1)
    by_run = world.fetch(
        engine,
        """SELECT j.title, d.outcome::text AS outcome, d.reason_codes::text[] AS reasons, d.rank_position
             FROM match_decisions d JOIN jobs j ON j.id = d.job_id WHERE d.match_run_id = :r ORDER BY j.title""",
        r=result.run_id,
    )
    assert [tuple(row) for row in by_run] == [
        ("Role A", "withheld", ["already_shown"], None),
        ("Role B", "shown", ["top_ranked"], 2),  # position 1 was taken by the other run
    ]


# --- no model, no consent -------------------------------------------------------------------------------------------


def test_with_no_provider_nothing_is_shown_and_every_retrieved_job_is_withheld_not_scored(engine, provider):
    calls = provider(LLM_PROVIDER="anthropic")  # the default provider with no key: AI is off
    user = world.add_person(engine)
    for name in "AB":
        world.add_job(engine, f"Role {name}")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (0, 2)
    assert [(d.outcome, d.reasons, d.evidence, d.fit_score) for d in decisions(engine, user)] == [
        ("withheld", ["not_scored"], 0, None)
    ] * 2
    run = run_row(engine, result.run_id)
    assert (run.status, run.evidence_prompt_version, run.retrieved_count) == ("completed", None, 2)
    assert calls == []


def test_without_the_ai_processing_consent_the_gateway_is_not_called_at_all(engine, provider, monkeypatch):
    calls = provider()
    asked = []
    monkeypatch.setattr(evidence, "score", lambda request, *, principal: asked.append(principal))
    user = world.add_person(engine, consent=False)
    world.add_job(engine, "Role A")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (0, 1) and asked == [] and calls == []
    (withheld,) = decisions(engine, user)
    assert (withheld.reasons, withheld.evidence) == (["not_scored"], 0)
    assert run_row(engine, result.run_id).evidence_prompt_version is None


def real_model(provider, monkeypatch):
    """A real model with a key (the anthropic route of models.toml), and a stand-in for the gateway that records who it
    was asked for and answers every requirement met by the same fact. Nothing reaches a model."""
    provider(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")
    asked: list[uuid.UUID] = []

    def answer(request, *, principal):
        asked.append(principal)
        return VerdictSet(
            verdicts=[Verdict(requirement=alias, verdict="met", facts=["f2"]) for alias in request.requirements]
        )

    monkeypatch.setattr(evidence, "score", answer)
    return asked


def test_a_real_model_no_gold_set_run_has_proven_is_never_asked_and_every_retrieved_job_is_withheld_not_scored(
    engine, provider, monkeypatch
):
    asked = real_model(provider, monkeypatch)
    user = world.add_person(engine)  # agreed to AI processing, and a key is set
    for name in "AB":
        world.add_job(engine, f"Role {name}")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert (result.shown, result.withheld) == (0, 2) and asked == []
    assert [(d.outcome, d.reasons, d.evidence) for d in decisions(engine, user)] == [
        ("withheld", ["not_scored"], 0)
    ] * 2
    run = run_row(engine, result.run_id)
    assert (run.status, run.evidence_prompt_version, run.retrieved_count) == ("completed", None, 2)


def test_a_real_model_with_a_passed_gold_set_run_is_asked_and_its_verdicts_are_shown(engine, provider, monkeypatch):
    asked = real_model(provider, monkeypatch)
    route = registry.route(evidence.TASK, config.get())
    with engine.begin() as c:  # what a live evidence_faithfulness run records when it passes (the worker role reads it)
        c.execute(
            text(
                """INSERT INTO eval_runs (suite, git_sha, config, status, metrics, is_gate_passed, finished_at)
                   VALUES ('evidence_faithfulness', 'abc', CAST(:config AS jsonb), 'passed', CAST('{}' AS jsonb), true,
                           now())"""
            ),
            {
                "config": json.dumps(
                    {"method": "model", "model": route.model, "prompt_version": evidence.PROMPT_VERSION}
                )
            },
        )
    user = world.add_person(engine)
    world.add_job(engine, "Role A")

    result = service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    assert result.shown == 1 and asked == [user]
    assert run_row(engine, result.run_id).evidence_prompt_version == evidence.PROMPT_VERSION


# --- failures -------------------------------------------------------------------------------------------------------


def test_provider_unavailable_fails_the_run_and_a_later_call_for_the_date_reuses_the_row(engine, provider, monkeypatch):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")
    real = evidence.score

    def down(request, *, principal):
        raise ProviderUnavailable()

    monkeypatch.setattr(evidence, "score", down)
    with pytest.raises(ProviderUnavailable):
        service.run(user, trigger="nightly", local_date=LOCAL_DATE)

    (failed,) = world.fetch(
        engine, "SELECT id, status::text AS status, error_code FROM match_runs WHERE user_id = :u", u=user
    )
    assert (failed.status, failed.error_code) == ("failed", "provider_unavailable")
    assert decisions(engine, user) == []

    monkeypatch.setattr(evidence, "score", real)
    result = service.run(user, trigger="nightly", local_date=LOCAL_DATE)

    assert result.run_id == failed.id and result.shown == 1
    (again,) = world.fetch(
        engine, "SELECT status::text AS status, error_code FROM match_runs WHERE user_id = :u", u=user
    )
    assert (again.status, again.error_code) == ("completed", None)


def test_any_other_failure_marks_the_run_internal_and_leaves_none_running(engine, provider, monkeypatch):
    provider()
    user = world.add_person(engine)
    world.add_job(engine, "Role A")

    def broken(request, *, principal):
        raise RuntimeError("invented failure")

    monkeypatch.setattr(evidence, "score", broken)
    with pytest.raises(RuntimeError):
        service.run(user, trigger="onboarding", local_date=LOCAL_DATE)

    (run,) = world.fetch(engine, "SELECT status::text AS status, error_code FROM match_runs WHERE user_id = :u", u=user)
    assert (run.status, run.error_code) == ("failed", "internal")
    assert decisions(engine, user) == []


# --- what is scored -------------------------------------------------------------------------------------------------


def test_a_job_without_requirements_and_jobs_beyond_the_score_limit_are_not_scored(engine, provider):
    calls = provider()
    user = world.add_person(engine)
    for name in "ABC":
        world.add_job(engine, f"Role {name}")
    world.add_job(engine, "Role D", requirements=())

    result = service.run(user, trigger="on_demand", local_date=LOCAL_DATE, score_limit=2)

    assert len(calls) == 2 and (result.shown, result.withheld) == (2, 2)
    by_title = {d.title: d for d in decisions(engine, user)}
    assert [by_title[t].outcome for t in ("Role A", "Role B")] == ["shown", "shown"]
    for title in ("Role C", "Role D"):
        assert (by_title[title].reasons, by_title[title].evidence) == (["not_scored"], 0)


def test_the_best_ranked_by_the_rules_are_the_ones_scored_when_there_is_a_limit(engine, provider, monkeypatch):
    provider()
    seen = []

    def answer_for(request):
        seen.append(request)
        return {"r1": ("no_evidence", []), "r2": ("no_evidence", [])}

    scripted(monkeypatch, answer_for)
    user = world.add_person(engine)
    with engine.begin() as c:
        c.execute(
            text("""INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id)
                    SELECT :u, 'target_title', 'staff engineer', 2, 'A staff engineer.', 'candidate_edit', id
                    FROM profile_events WHERE user_id = :u LIMIT 1"""),
            {"u": user},
        )
    world.add_job(engine, "Role plain")
    wanted = world.add_job(engine, "Staff engineer, platform")

    service.run(user, trigger="on_demand", local_date=LOCAL_DATE, score_limit=1)

    assert [r.id for r in seen[0].requirements.values()] == list(wanted.requirement_ids)


def test_a_run_for_a_person_who_cannot_be_matched_is_refused(engine, provider):
    provider()
    with pytest.raises(service.NotRunnable):
        service.run(uuid.uuid4(), trigger="nightly", local_date=LOCAL_DATE)
    user = world.add_person(engine)
    with pytest.raises(ValueError):
        service.run(user, trigger="role", local_date=LOCAL_DATE)


def test_the_entry_points_other_modules_call_are_exported():
    assert api.run is service.run and api.RANKER_VERSION == ranking.RANKER_VERSION
    assert callable(api.retrieve) and callable(api.violations)
