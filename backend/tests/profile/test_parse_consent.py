"""Plan 03-05, task 2: without the ai_processing consent no model reads anything, every AI path says why it is off, and
the rest of the app keeps working (TRU-05, success criterion 1; threat T-3-17).

The consent is checked twice: before the upload route reads the body, and again in the job, because the person may
withdraw it between the two. The wording a consent was given under names the providers, so a changed provider list
makes an old consent stale (trust.consent); the job sees that as a missing consent too.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.profile import documents_routes
from app.profile.documents_routes import MAX_REQUEST_BYTES

URL = "/v1/me/documents"
WITHDRAW_AI = {"consents": [{"purpose": "ai_processing", "granted": False}]}


def upload(client, data, name="resume.docx"):
    return client.post(
        URL, files={"file": (name, data, "application/octet-stream")}, headers={"Idempotency-Key": uuid4().hex}
    )


def send(client, method, path, body):
    return client.request(method, path, json=body, headers={"Idempotency-Key": uuid4().hex})


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


def nothing_stored(kit):
    assert (kit.count("documents"), kit.count("queue_jobs"), kit.count("llm_calls")) == (0, 0, 0)


@pytest.fixture
def with_a_provider(signed_in, kit):
    """A signed-in person who has not agreed to anything, on a deployment that has a model provider."""
    kit.provider("fake")
    return signed_in


# ---- the upload route ----------------------------------------------------------------------------


def test_without_consent_the_upload_is_refused_with_a_code_and_the_reason_in_words(with_a_provider, kit):
    error = refused(upload(with_a_provider, kit.docx()), 403, "consent_required")
    assert error["details"] == {"purpose": "ai_processing", "reason": "missing"}
    assert "have not agreed to AI processing" in error["message"]  # what is off, and why
    nothing_stored(kit)


def test_a_consent_given_under_other_wording_is_refused_as_providers_changed(with_a_provider, kit):
    kit.agree(with_a_provider.user, version="old-wording-1")
    error = refused(upload(with_a_provider, kit.docx()), 403, "consent_required")
    assert error["details"] == {"purpose": "ai_processing", "reason": "providers_changed"}
    assert "providers" in error["message"]
    nothing_stored(kit)


def test_the_consent_is_checked_before_the_body_is_read(with_a_provider, kit):
    huge = b"%PDF-" + bytes(MAX_REQUEST_BYTES)  # over the size cap: 413 if anything had read the body first
    refused(upload(with_a_provider, huge), 403, "consent_required")
    nothing_stored(kit)


def test_the_rest_of_the_app_works_for_a_person_who_has_not_agreed(with_a_provider, kit):
    client = with_a_provider
    refused(upload(client, kit.docx()), 403, "consent_required")  # the one thing that needs a model

    assert ok(client.get("/v1/me/record"))["facts"] == []
    fact = ok(send(client, "POST", "/v1/me/facts", {"kind": "skill", "statement": "Rust, three years"}), 201)["fact"]
    remote = {"kind": "remote_policy", "arrangements": ["remote"], "statement": "Remote work only"}
    ok(send(client, "POST", "/v1/me/constraints", remote), 201)
    record = ok(client.get("/v1/me/record"))
    assert [f["statement"] for f in record["facts"]] == [fact["statement"]]
    assert [c["kind"] for c in record["constraints"]] == ["remote_policy"]
    assert client.get("/v1/jobs").status_code == 200  # search is open to everyone


def test_with_no_model_configured_the_upload_says_so_before_it_asks_for_a_consent(signed_in, kit):
    kit.provider(None)
    for agreed in (False, True):  # a consent changes nothing when there is no model to use it
        if agreed:
            kit.agree(signed_in.user)
        error = refused(upload(signed_in, kit.docx()), 503, "ai_disabled")
        assert "needs an AI model" in error["message"]
    nothing_stored(kit)
    assert ok(signed_in.get("/v1/me/record"))["facts"] == []  # the record still works


def test_every_resume_task_needs_a_provider_not_only_the_first(signed_in, kit, monkeypatch):
    kit.provider("local")  # a server that serves exactly the tasks models.toml names for it
    kit.agree(signed_in.user)
    assert ok(upload(signed_in, kit.docx()), 202)["status"] == "queued"  # all three resume tasks are served
    one_more = SimpleNamespace(name="resume.facts.not_configured")
    monkeypatch.setattr(documents_routes, "TASKS", (*documents_routes.TASKS, one_more))
    refused(upload(signed_in, kit.docx()), 503, "ai_disabled")
    assert kit.count("documents") == 1  # only the first upload


def test_with_a_provider_and_a_consent_the_upload_is_accepted(with_a_provider, kit):
    kit.agree(with_a_provider.user)
    assert ok(upload(with_a_provider, kit.docx()), 202)["status"] == "queued"


# ---- the job -------------------------------------------------------------------------------------


def test_a_consent_withdrawn_after_the_upload_stops_the_parse_before_the_model_is_called(with_a_provider, kit):
    kit.agree(with_a_provider.user)
    created = ok(upload(with_a_provider, kit.docx()), 202)
    ok(send(with_a_provider, "PATCH", "/v1/me", WITHDRAW_AI))  # the person withdraws before the worker gets to it
    model = kit.model()
    assert kit.run(model) == 1
    assert model.calls == [] and kit.facts(created["id"]) == []
    data = ok(with_a_provider.get(f"{URL}/{created['id']}"))
    assert (data["status"], data["parse_error_code"]) == ("failed", "consent_required")
    assert "have not agreed to AI processing" in data["message"]
    assert (kit.job(created["id"]).state, kit.job(created["id"]).attempts) == ("succeeded", 1)  # done, not retried


def test_agreeing_again_before_the_job_runs_lets_the_parse_go_ahead(with_a_provider, kit):
    kit.agree(with_a_provider.user)
    created = ok(upload(with_a_provider, kit.docx()), 202)
    kit.withdraw(with_a_provider.user)
    kit.agree(with_a_provider.user)  # the consent is read when the job runs, not when the file arrived
    model = kit.model()
    assert kit.run(model) == 1
    assert len(model.calls) == 3 and kit.document(created["id"]).state == "parsed"


def test_a_provider_change_after_the_upload_makes_the_consent_stale_for_the_job(with_a_provider, kit):
    kit.agree(with_a_provider.user)
    created = ok(upload(with_a_provider, kit.docx()), 202)
    kit.provider("local")  # another provider is named in the wording now: the old agreement no longer covers it
    model = kit.model()
    assert kit.run(model) == 1
    document = kit.document(created["id"])
    assert (document.state, document.parse_error_code) == ("failed", "consent_required")
    assert model.calls == [] and kit.facts(created["id"]) == []


def test_withdrawing_stops_new_uploads_and_keeps_what_was_already_read(with_a_provider, kit):
    kit.agree(with_a_provider.user)
    created = ok(upload(with_a_provider, kit.docx()), 202)
    assert kit.run(kit.model()) == 1
    ok(send(with_a_provider, "PATCH", "/v1/me", WITHDRAW_AI))
    refused(upload(with_a_provider, kit.docx()), 403, "consent_required")
    facts = ok(with_a_provider.get("/v1/me/facts", params={"status": "unconfirmed", "limit": 100}))
    assert len(facts) == kit.KEPT and {f["provenance"]["document_id"] for f in facts} == {created["id"]}
