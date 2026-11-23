"""TLR-01, TLR-02 (plan 05-05): asking for a tailored resume, the tailor.generate job, and the read.

Everything is invented. The real gateway runs on recordings (tests/tailor/fixtures/service/harness.py); the job handler
is called directly on a worker connection, no worker process. Aliases are those of profile.api.usable_facts order:
`w.alias["bullet_initech_1"]` is the alias the request gave that fact.
"""

from uuid import UUID

import pytest
from sqlalchemy import text

from app.platform import db
from app.platform.llm import ProviderUnavailable, SchemaInvalid, fake
from app.platform.queue import NonRetryable
from app.tailor import pipeline, service
from app.trust import api as trust
from tests.p5_factories import make_job, make_tailored_resume, make_user
from tests.tailor.fixtures.service import harness
from tests.tailor.fixtures.service.harness import edit, key, sign_in


@pytest.fixture
def w(engine, api_client, monkeypatch):
    harness.empty_tables(engine)
    harness.use_provider(monkeypatch)  # the fake provider; a test that needs answers replaces it
    world = harness.world(engine)  # after it: a consent is for the providers in force when it is given
    world.client, world.engine, world.monkeypatch = api_client, engine, monkeypatch
    sign_in(api_client, world.ada)
    yield world
    fake.replay_from(None)
    harness.empty_tables(engine)


def ok(response, status=200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def ask(w, job_id=None, headers=None):
    return w.client.post("/v1/tailored-resumes", json={"job_id": job_id or w.job.id}, headers=headers or key())


def run_job(tailored_id):
    """The queue handler, as the worker calls it."""
    with db.transaction(role="worker") as conn:
        service.generate(conn, {"tailored_resume_id": str(tailored_id)})


def recording(w, tmp_path, edits, *rest):
    path = harness.write_answers(tmp_path, {"edits": edits}, *rest)
    return harness.use_provider(w.monkeypatch, path, TAILOR_REPHRASE="true")


def mixed(w):
    """Two edits that hold up and three that do not: an inflated number, a job-text skill, a paraphrased add."""
    a = w.alias
    return [
        edit("reorder", order=(a["skill_quillscript"], a["skill_python"], a["skill_postgresql"]), section="skills"),
        edit("remove", target=a["bullet_globex_2"], supporting=(a["bullet_globex_2"],)),
        edit(
            "rephrase",
            target=a["bullet_initech_1"],
            after="Built nightly Python and PostgreSQL pipelines that cut report time by 60 percent.",
            supporting=(a["bullet_initech_1"],),
        ),
        edit(
            "rephrase",
            target=a["bullet_globex_1"],
            after="Wrote SQL reports for the finance team on Kubernetes.",
            supporting=(a["bullet_globex_1"],),
        ),
        edit(
            "add_from_fact", after="Python skills of the first order", supporting=(a["skill_python"],), section="skills"
        ),
    ]


def rows(engine, sql, **params):
    with engine.connect() as c:
        return list(c.execute(text(sql), params))


# ---- the request ----------------------------------------


def test_a_request_is_202_generating_and_queues_one_job(w):
    data = ok(ask(w), 202)
    assert (data["status"], data["job_id"], data["edits"], data["discarded_count"]) == ("generating", w.job.id, [], 0)
    queued = rows(w.engine, "SELECT kind, queue, unique_key, user_id FROM queue_jobs")
    assert [tuple(q) for q in queued] == [("tailor.generate", "interactive", f"tailor.generate:{data['id']}", w.ada)]
    stored = rows(
        w.engine, "SELECT job_content_version, record_version, base_document_id, status FROM tailored_resumes"
    )
    assert (stored[0].job_content_version, stored[0].record_version, stored[0].base_document_id, stored[0].status) == (
        1,
        1,
        w.record.document_id,
        "generating",
    )


def test_the_same_idempotency_key_returns_the_same_tailored_resume(w):
    headers = key()
    first = ok(ask(w, headers=headers), 202)
    again = w.client.post("/v1/tailored-resumes", json={"job_id": w.job.id}, headers=headers)
    assert ok(again, 202)["id"] == first["id"]
    assert rows(w.engine, "SELECT count(*) AS n FROM tailored_resumes")[0].n == 1


def test_without_the_ai_processing_consent_it_is_403(w):
    with w.engine.begin() as c:
        trust.withdraw_consent(c, w.ada, "ai_processing")
    harness.use_provider(w.monkeypatch)
    error = refused(ask(w), 403, "consent_required")
    assert error["details"]["purpose"] == "ai_processing"
    assert rows(w.engine, "SELECT count(*) AS n FROM tailored_resumes")[0].n == 0


def test_with_no_route_it_is_503_ai_disabled(w):
    harness.use_provider(w.monkeypatch, LLM_PROVIDER="anthropic")  # no key: no route
    refused(ask(w), 503, "ai_disabled")
    assert rows(w.engine, "SELECT count(*) AS n FROM queue_jobs")[0].n == 0


def test_the_eleventh_request_in_a_day_is_429_with_the_wait(w):
    harness.use_provider(w.monkeypatch)
    with w.engine.begin() as c:
        for _ in range(service.TAILORED_PER_DAY):
            make_tailored_resume(c, w.ada, w.job)
    response = ask(w)
    error = refused(response, 429, "rate_limited")
    assert 0 < error["details"]["retry_after_seconds"] <= 24 * 3600
    assert response.headers["Retry-After"] == str(error["details"]["retry_after_seconds"])
    # another person is not held up by Ada's count
    sign_in(w.client, w.bob)
    ok(ask(w), 202)


def test_a_closed_job_is_410(w):
    harness.use_provider(w.monkeypatch)
    with w.engine.begin() as c:
        c.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": w.job.id})
    refused(ask(w), 410, "job_closed")


def test_a_person_with_no_parsed_resume_is_409_no_resume(w):
    harness.use_provider(w.monkeypatch)
    with w.engine.begin() as c:
        newcomer = make_user(c)
        trust.grant_consent(c, newcomer, "ai_processing", trust.ai_processing_text_version())
    sign_in(w.client, newcomer)
    error = refused(ask(w), 409, "conflict")
    assert error["details"]["state"] == "no_resume"


def test_an_unknown_job_is_404(w):
    harness.use_provider(w.monkeypatch)
    refused(ask(w, job_id=w.job.id + 1000), 404, "not_found")


# ---- generating ----------------------------------------


def test_the_job_stores_five_edits_two_passed_and_three_discarded_for_their_reasons(w, tmp_path):
    calls = recording(w, tmp_path, mixed(w))
    tailored = UUID(ok(ask(w), 202)["id"])
    run_job(tailored)
    stored = rows(
        w.engine,
        """SELECT ordinal, verifier_status::text AS status, discard_reason::text AS reason, supporting_fact_ids
             FROM resume_edits WHERE tailored_resume_id = :t ORDER BY ordinal""",
        t=tailored,
    )
    assert [(r.ordinal, r.status, r.reason) for r in stored] == [
        (1, "passed", None),
        (2, "passed", None),
        (3, "discarded", "changed_number"),
        (4, "discarded", "new_entity"),
        (5, "discarded", "no_supporting_fact"),
    ]
    assert all(r.supporting_fact_ids for r in stored[:2])
    head = rows(w.engine, "SELECT status::text AS s, discarded_count, prompt_version FROM tailored_resumes")[0]
    assert (head.s, head.discarded_count, head.prompt_version) == ("ready", 3, pipeline.PROPOSE_VERSION)
    assert [c[0] for c in calls] == ["tailor.propose"]  # nothing to entail: no rephrase passed layer 1


def test_the_read_shows_passed_edits_and_counts_never_the_text_of_a_discarded_one(w, tmp_path):
    recording(w, tmp_path, mixed(w))
    tailored = ok(ask(w), 202)["id"]
    run_job(tailored)
    response = w.client.get(f"/v1/tailored-resumes/{tailored}")
    data = ok(response)
    assert (data["status"], data["discarded_count"]) == ("ready", 3)
    assert data["discarded_by_reason"] == {"changed_number": 1, "new_entity": 1, "no_supporting_fact": 1}
    assert [(e["ordinal"], e["op"], e["decision"]) for e in data["edits"]] == [
        (1, "reorder", "pending"),
        (2, "remove", "pending"),
    ]
    assert all(e["supporting_fact_ids"] for e in data["edits"])
    assert (data["job_title"], data["company_name"]) == ("Data engineer", "Initech Demo")
    for secret in ("60 percent", "Kubernetes", "first order"):
        assert secret not in response.text
    # a discarded edit's id appears nowhere either
    with w.engine.connect() as c:
        discarded = c.execute(text("SELECT id FROM resume_edits WHERE verifier_status = 'discarded'")).scalars().all()
    assert discarded and all(str(i) not in response.text for i in discarded)


def test_when_every_edit_is_unsupported_the_resume_is_ready_with_no_edits(w, tmp_path):
    a = w.alias
    bad = mixed(w)[2:]
    recording(w, tmp_path, bad)
    tailored = ok(ask(w), 202)["id"]
    run_job(tailored)
    data = ok(w.client.get(f"/v1/tailored-resumes/{tailored}"))
    assert (data["status"], data["edits"], data["discarded_count"]) == ("ready", [], len(bad))
    assert a  # aliases were used by mixed()


def test_the_rephrase_flag_is_passed_on(w, monkeypatch):
    seen = []

    def spy(facts, requirements, verdicts, *, principal, allow_rephrase):
        seen.append(allow_rephrase)
        return pipeline.Proposal([], "v")

    monkeypatch.setattr(service.pipeline, "propose_and_verify", spy)
    for flag in ("false", "true"):
        harness.use_provider(monkeypatch, TAILOR_REPHRASE=flag)
        run_job(UUID(ok(ask(w), 202)["id"]))
        with w.engine.begin() as c:
            c.execute(text("UPDATE tailored_resumes SET created_at = created_at - interval '2 days'"))
    assert seen == [False, True]


# ---- failing ----------------------------------------


def started(w):
    return UUID(ok(ask(w), 202)["id"])


def status_of(w, tailored):
    return ok(w.client.get(f"/v1/tailored-resumes/{tailored}"))


def test_ai_turned_off_when_the_job_runs_fails_it_without_a_retry(w):
    harness.use_provider(w.monkeypatch)  # the fake provider with no recording: AIDisabled at run time
    tailored = started(w)
    with pytest.raises(NonRetryable):
        run_job(tailored)
    data = status_of(w, tailored)
    assert (data["status"], data["edits"], data["discarded_count"]) == ("failed", [], 0)


def test_consent_withdrawn_before_the_job_runs_fails_it_without_a_retry(w, tmp_path):
    recording(w, tmp_path, [])
    tailored = started(w)
    with w.engine.begin() as c:
        trust.withdraw_consent(c, w.ada, "ai_processing")
    with pytest.raises(NonRetryable):
        run_job(tailored)
    assert status_of(w, tailored)["status"] == "failed"
    assert [c for c in rows(w.engine, "SELECT 1 FROM resume_edits")] == []


def test_an_answer_that_stays_invalid_fails_it_without_a_retry(w):
    harness.use_provider(w.monkeypatch)

    def invalid(*args, **kwargs):
        raise SchemaInvalid()

    w.monkeypatch.setattr(service.pipeline, "propose_and_verify", invalid)
    tailored = started(w)
    with pytest.raises(NonRetryable):
        run_job(tailored)
    assert status_of(w, tailored)["status"] == "failed"


def test_a_provider_that_does_not_answer_is_left_for_the_queue_to_retry(w):
    harness.use_provider(w.monkeypatch)

    def down(*args, **kwargs):
        raise ProviderUnavailable()

    w.monkeypatch.setattr(service.pipeline, "propose_and_verify", down)
    tailored = started(w)
    with pytest.raises(ProviderUnavailable):
        run_job(tailored)
    assert status_of(w, tailored)["status"] == "generating"
    # on its last attempt it is failed for good, and the error still goes on to the queue
    with w.engine.begin() as c:
        c.execute(text("UPDATE queue_jobs SET attempts = max_attempts"))
    with pytest.raises(ProviderUnavailable):
        run_job(tailored)
    assert status_of(w, tailored)["status"] == "failed"


def test_a_dead_queue_job_reads_as_failed(w):
    tailored = started(w)
    with w.engine.begin() as c:
        c.execute(text("UPDATE queue_jobs SET status = 'dead'"))
    assert status_of(w, tailored)["status"] == "failed"


def test_invariant_three_holds_at_the_api_even_when_the_verifier_is_broken(w, monkeypatch):
    """A passed edit resting on an unconfirmed fact: the database refuses the insert, the tailored resume fails and no
    edit of the run can be read."""
    harness.use_provider(monkeypatch)
    with w.engine.begin() as c:
        unconfirmed = c.execute(
            text(
                """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, document_id, source_span,
                                              source_quote)
                   VALUES (:u, 'skill', 'Python', '{"name": "Python"}', 'resume', :doc,
                           int4range(0, 6), 'Ada Ex') RETURNING id"""
            ),
            {"u": w.ada, "doc": w.record.document_id},
        ).scalar_one()
    broken = pipeline.Proposal(
        [
            pipeline.VerifiedEdit(
                1,
                "remove",
                "experience",
                str(w.record.facts["bullet_globex_2"]),
                "x",
                "",
                (str(w.record.facts["bullet_globex_2"]),),
                "passed",
                None,
            ),
            pipeline.VerifiedEdit(
                2,
                "add_from_fact",
                "skills",
                None,
                "",
                "Python",
                (str(unconfirmed),),
                "passed",
                None,
            ),
        ],
        "v",
    )
    monkeypatch.setattr(service.pipeline, "propose_and_verify", lambda *a, **k: broken)
    tailored = started(w)
    with pytest.raises(NonRetryable):
        run_job(tailored)
    data = status_of(w, tailored)
    assert (data["status"], data["edits"]) == ("failed", [])
    assert rows(w.engine, "SELECT count(*) AS n FROM resume_edits")[0].n == 0  # the good edit rolled back with it


# ---- listing and ownership ----------------------------------------


def test_the_list_holds_only_the_callers_newest_first_and_another_persons_id_is_404(w):
    harness.use_provider(w.monkeypatch)
    with w.engine.begin() as c:
        other = make_job(c, title="Analyst", description=harness.JOB_TEXT, requirements=harness.REQUIREMENTS)
        first = make_tailored_resume(c, w.ada, w.job)
        second = make_tailored_resume(c, w.ada, other)
        bobs = make_tailored_resume(c, w.bob, w.job)
    listed = ok(w.client.get("/v1/tailored-resumes"))
    assert [item["id"] for item in listed] == [str(second), str(first)]
    refused(w.client.get(f"/v1/tailored-resumes/{bobs}"), 404, "not_found")
    sign_in(w.client, w.bob)
    assert [item["id"] for item in ok(w.client.get("/v1/tailored-resumes"))] == [str(bobs)]
    refused(w.client.get(f"/v1/tailored-resumes/{first}"), 404, "not_found")


def test_no_tailoring_path_writes_the_outbox(w, tmp_path):
    recording(w, tmp_path, mixed(w))
    run_job(ok(ask(w), 202)["id"])
    assert rows(w.engine, "SELECT count(*) AS n FROM outbox")[0].n == 0
