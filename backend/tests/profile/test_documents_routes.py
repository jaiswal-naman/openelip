"""Plan 03-05, task 2: the upload and status routes (REC-01, REC-06; threats T-3-16 and T-3-17).

The API opens its own connections as the real app_api role, so row-level security and the grants are in force. The
person is signed in by overriding the session dependency, as the other route tests do. A parse runs through the real
queue handler with the scripted model of conftest.py; consent has its own file (test_parse_consent.py).
"""

import asyncio
import io
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from tempfile import SpooledTemporaryFile
from uuid import uuid4

import pytest
from sqlalchemy import text
from starlette import formparsers

from app.platform import db
from app.platform.errors import ApiError
from app.platform.llm import SchemaInvalid
from app.platform.sessions import open_sealed_bytes
from app.profile import documents, documents_routes
from app.profile.documents import MAX_BYTES, PER_DAY, PayloadTooLarge
from app.profile.documents_routes import MAX_REQUEST_BYTES, capped
from app.profile.extract import workflow
from app.profile.resume.reader import DOCX_MIME, PDF_MIME

ROLES, EDUCATION_SKILLS, SUMMARY = (task.name for task in workflow.TASKS)
URL = "/v1/me/documents"


def upload(client, data, name="resume.docx", key=None):
    return client.post(
        URL, files={"file": (name, data, "application/octet-stream")}, headers={"Idempotency-Key": key or uuid4().hex}
    )


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


def a_zip(*names, body=b"x"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in names:
            archive.writestr(name, body)
    return out.getvalue()


def nothing_stored(kit):
    assert (kit.count("documents"), kit.count("queue_jobs")) == (0, 0)


@pytest.fixture
def uploader(signed_in, kit):
    """A signed-in person who has agreed to AI processing, with a model provider configured."""
    kit.provider("fake")
    kit.agree(signed_in.user)
    return signed_in


# ---- POST /v1/me/documents -----------------------------------------------------------------------


def test_an_upload_is_kept_sealed_and_answers_202_with_the_id_and_where_to_poll(uploader, kit):
    data = kit.docx()
    response = upload(uploader, data, "Mara CV.docx")
    body = ok(response, 202)
    assert set(body) == {"id", "status"} and body["status"] == "queued"
    assert response.headers["Location"] == f"/v1/me/documents/{body['id']}"
    row = kit.document(body["id"])
    assert (row.state, row.user_id, row.mime_type, row.original_filename) == (
        "uploaded",
        uploader.user,
        DOCX_MIME,
        "Mara CV.docx",
    )
    assert open_sealed_bytes(bytes(row.content)) == data
    assert (kit.job(body["id"]).state, kit.count("queue_jobs")) == ("queued", 1)
    assert kit.job(body["id"]).user_id == uploader.user  # the job is the person's: exported with them, erased with them


def test_the_same_key_and_file_answer_with_the_first_answer_and_store_nothing_more(uploader, kit):
    key, data = uuid4().hex, kit.docx()
    first = upload(uploader, data, key=key)
    again = upload(uploader, data, key=key)
    assert again.status_code == 202 and again.json()["data"] == first.json()["data"]
    assert again.json()["meta"]["idempotent_replay"] is True
    assert again.headers["Location"] == first.headers["Location"]
    assert (kit.count("documents"), kit.count("queue_jobs")) == (1, 1)
    refused(upload(uploader, kit.pdf(), key=key), 422, "idempotency_key_reused")  # same key, another file
    assert kit.count("documents") == 1


def test_every_upload_needs_an_idempotency_key(uploader, kit):
    data = kit.docx()
    without = uploader.post(URL, files={"file": ("resume.docx", data, "application/octet-stream")})
    refused(without, 400, "idempotency_key_missing")
    refused(upload(uploader, data, key="short"), 400, "invalid_request")
    nothing_stored(kit)


def test_an_upload_needs_a_session(api_client, kit):
    refused(upload(api_client, kit.docx()), 401, "unauthenticated")
    nothing_stored(kit)


def test_a_file_one_byte_over_ten_megabytes_is_refused_with_the_limit(uploader, kit):
    error = refused(upload(uploader, b"%PDF-" + bytes(MAX_BYTES - 4)), 413, "payload_too_large")
    assert error["details"] == {"max_bytes": MAX_BYTES}
    nothing_stored(kit)


def test_a_request_far_over_the_cap_is_refused_and_stores_nothing(uploader, kit):
    refused(upload(uploader, b"%PDF-" + bytes(MAX_REQUEST_BYTES)), 413, "payload_too_large")
    nothing_stored(kit)


def test_the_route_applies_the_cap_to_the_request_while_it_streams(uploader, kit, monkeypatch):
    monkeypatch.setattr(documents_routes, "MAX_REQUEST_BYTES", 2_000)  # a small file, but over this cap
    refused(upload(uploader, kit.docx()), 413, "payload_too_large")
    nothing_stored(kit)


def test_a_large_upload_stays_in_memory_and_never_becomes_a_temporary_file(uploader, kit, monkeypatch):
    rolled = []

    class Watched(SpooledTemporaryFile):
        def close(self):
            rolled.append(self._rolled)  # True once the parser has moved the file from memory to disk
            super().close()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Watched)
    three_megabytes = a_zip("word/document.xml", body=os.urandom(3_000_000))  # does not compress; the type is a DOCX's
    ok(upload(uploader, three_megabytes), 202)
    assert rolled and set(rolled) == {False}


def test_the_stream_is_cut_off_where_it_crosses_the_cap_and_not_read_to_the_end():
    pulled = []

    async def chunks():
        for number in range(1000):
            pulled.append(number)
            yield b"x" * 100

    async def read_everything():
        return [chunk async for chunk in capped(chunks(), 1000)]

    with pytest.raises(PayloadTooLarge):
        asyncio.run(read_everything())
    assert len(pulled) == 11  # ten chunks fit; the eleventh crossed the cap and nothing after it was asked for


NOT_RESUMES = {
    "empty": b"",
    "plain text": b"just some text",
    "zip with no word document": a_zip("notes.txt"),
    "program": b"MZ" + bytes(100),
}


@pytest.mark.parametrize("data", NOT_RESUMES.values(), ids=NOT_RESUMES.keys())
def test_a_file_that_is_not_a_pdf_or_docx_is_refused_whatever_it_claims_to_be(uploader, kit, data):
    response = uploader.post(
        URL, files={"file": ("resume.pdf", data, "application/pdf")}, headers={"Idempotency-Key": uuid4().hex}
    )
    error = refused(response, 415, "unsupported_media_type")
    assert error["details"]["accepted"] == [PDF_MIME, DOCX_MIME]
    nothing_stored(kit)


def test_a_body_that_is_not_multipart_is_refused(uploader, kit):
    response = uploader.post(URL, json={"file": "x"}, headers={"Idempotency-Key": uuid4().hex})
    refused(response, 415, "unsupported_media_type")
    nothing_stored(kit)


def test_an_upload_that_is_not_one_file_in_the_file_field_is_refused(uploader, kit):
    data, key = kit.docx(), {"Idempotency-Key": uuid4().hex}
    no_boundary = uploader.post(URL, content=b"abc", headers={**key, "Content-Type": "multipart/form-data"})
    refused(no_boundary, 400, "invalid_request")
    wrong_name = uploader.post(URL, files={"resume": ("a.docx", data)}, headers=key)
    assert refused(wrong_name, 422, "validation_failed")["details"]["fields"] == [
        {"field": "file", "issue": "required"}
    ]
    two_files = uploader.post(URL, files=[("file", ("a.docx", data)), ("file", ("b.docx", data))], headers=key)
    refused(two_files, 400, "invalid_request")
    with_a_note = uploader.post(URL, files={"file": ("a.docx", data)}, data={"note": "hello"}, headers=key)
    refused(with_a_note, 400, "invalid_request")
    nothing_stored(kit)


def test_the_upload_is_described_for_the_generated_web_types(api_client):
    document = api_client.get("/openapi.json").json()
    post = document["paths"][URL]["post"]
    assert post["requestBody"]["required"] is True
    assert post["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"]["file"]["format"] == "binary"
    assert "202" in post["responses"] and {p["name"] for p in post["parameters"]} == {"idempotency-key"}
    assert {p["name"] for p in document["paths"][URL + "/{document_id}"]["get"]["parameters"]} == {
        "document_id",
        "include",
    }
    schemas = document["components"]["schemas"]  # text that came from the candidate's file is marked for the clients
    assert schemas["DocumentView"]["properties"]["text"]["x-untrusted-text"] is True
    assert schemas["DocumentView"]["properties"]["original_filename"]["x-untrusted-text"] is True
    assert schemas["DocumentLine"]["properties"]["text"]["x-untrusted-text"] is True
    assert schemas["DocumentHiddenText"]["properties"]["samples"]["x-untrusted-text"] is True


# ---- the daily limit: 10 resume uploads per person per day (API.md 1.8) ---------------------------


def store_many(kit, user, count):
    data = kit.docx()
    for _ in range(count):
        kit.store(user, data)


def age_the_oldest_upload(kit, interval):
    """Make the oldest document older, as if it had been uploaded `interval` ago."""
    with kit.engine.begin() as c:
        c.execute(
            text(
                """UPDATE documents SET created_at = now() - CAST(:interval AS interval)
                   WHERE id = (SELECT id FROM documents ORDER BY created_at LIMIT 1)"""
            ),
            {"interval": interval},
        )


def test_the_eleventh_upload_in_a_day_is_refused_with_429_the_wait_and_a_retry_after_header(uploader, kit):
    for _ in range(PER_DAY):
        ok(upload(uploader, kit.docx()), 202)

    response = upload(uploader, kit.docx())

    error = refused(response, 429, "rate_limited")
    wait = error["details"]["retry_after_seconds"]
    assert 86_000 < wait <= 86_400  # the first upload of the day has just been made: about a day to wait
    assert response.headers["Retry-After"] == str(wait)  # API.md 1.8: a refusal carries Retry-After, in seconds
    assert (kit.count("documents"), kit.count("queue_jobs")) == (PER_DAY, PER_DAY)  # nothing stored for the eleventh


def test_the_limit_is_per_person(uploader, kit):
    store_many(kit, uploader.user, PER_DAY)

    refused(upload(uploader, kit.docx()), 429, "rate_limited")
    kit.store(kit.person("bob@example.test"), kit.docx())  # someone else's first upload goes through

    assert kit.count("documents") == PER_DAY + 1


def test_the_wait_ends_when_the_oldest_upload_of_the_day_is_a_day_old(uploader, kit):
    store_many(kit, uploader.user, PER_DAY)
    age_the_oldest_upload(kit, "23 hours 50 minutes")

    error = refused(upload(uploader, kit.docx()), 429, "rate_limited")
    assert 540 <= error["details"]["retry_after_seconds"] <= 660  # about ten minutes

    age_the_oldest_upload(kit, "25 hours")  # now it no longer counts
    ok(upload(uploader, kit.docx()), 202)
    assert kit.count("documents") == PER_DAY + 1


def test_a_refused_upload_leaves_its_key_free_for_the_retry(uploader, kit):
    store_many(kit, uploader.user, PER_DAY)
    key, data = uuid4().hex, kit.docx()
    refused(upload(uploader, data, key=key), 429, "rate_limited")

    age_the_oldest_upload(kit, "25 hours")
    retry = upload(uploader, data, key=key)

    assert ok(retry, 202)["status"] == "queued"
    assert "idempotent_replay" not in retry.json()["meta"]  # a new upload, not the stored refusal played back


def test_a_file_that_is_refused_for_its_type_does_not_count_towards_the_day(uploader, kit):
    for _ in range(3):
        refused(upload(uploader, b"just some text"), 415, "unsupported_media_type")
    store_many(kit, uploader.user, PER_DAY)  # all ten still fit
    assert kit.count("documents") == PER_DAY


def test_two_uploads_at_the_same_moment_cannot_both_take_the_last_place(kit, monkeypatch):
    """Without a lock on the person each transaction counts nine, and all of them insert. The seal is slowed down so the
    transactions overlap for certain: the count is made before it and the insert after it."""
    user, data = kit.person(), kit.docx()
    store_many(kit, user, PER_DAY - 1)
    real_seal = documents.seal_bytes

    def slow_seal(content):
        time.sleep(0.3)
        return real_seal(content)

    monkeypatch.setattr(documents, "seal_bytes", slow_seal)

    def try_one(_):
        try:
            with db.transaction(user) as conn:
                return documents.store_document(conn, user, "resume.docx", data)
        except ApiError as refusal:
            return refusal

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(try_one, range(4)))

    refusals = [r for r in results if isinstance(r, ApiError)]
    assert len(refusals) == 3 and all((r.status, r.code) == (429, "rate_limited") for r in refusals)
    assert kit.count("documents") == PER_DAY


# ---- GET /v1/me/documents/{id} -------------------------------------------------------------------


def test_a_document_waiting_for_the_worker_says_queued_and_when_to_ask_again(uploader, kit):
    created = ok(upload(uploader, kit.docx(), "cv.docx"), 202)
    response = uploader.get(f"{URL}/{created['id']}")
    data = ok(response)
    assert response.headers["Retry-After"] == "2"
    assert (data["id"], data["status"], data["original_filename"], data["mime_type"]) == (
        created["id"],
        "queued",
        "cv.docx",
        DOCX_MIME,
    )
    assert (data["parse_error_code"], data["message"], data["hidden_text_flags"], data["parsed_at"]) == (
        None,
        None,
        None,
        None,
    )
    assert data["facts"] == {"kept": 0, "unverified": 0, "to_review": 0}
    assert (data["text"], data["lines"], data["hidden_text_chars"]) == (None, None, 0)


def test_a_document_the_worker_has_claimed_says_running(uploader, kit):
    created = ok(upload(uploader, kit.docx()), 202)
    with kit.engine.begin() as c:
        c.execute(text("UPDATE documents SET status = 'parsing' WHERE id = :id"), {"id": created["id"]})
    response = uploader.get(f"{URL}/{created['id']}")
    assert ok(response)["status"] == "running" and response.headers["Retry-After"] == "2"


def test_a_parsed_document_reports_its_facts_and_the_text_removed_from_it(uploader, kit):
    created = ok(upload(uploader, kit.docx(hidden=kit.HIDDEN)), 202)
    assert kit.run(kit.model()) == 1
    response = uploader.get(f"{URL}/{created['id']}")
    data = ok(response)
    assert "Retry-After" not in response.headers
    assert (data["status"], data["parse_error_code"], data["message"]) == ("succeeded", None, None)
    assert data["facts"] == {"kept": kit.KEPT, "unverified": 0, "to_review": kit.KEPT}
    assert data["hidden_text_chars"] == len(kit.HIDDEN.replace(" ", ""))
    assert data["hidden_text_flags"] == {"reasons": ["docx_hidden_run"], "samples": [kit.HIDDEN[:40]]}
    assert data["parsed_at"] is not None and data["text"] is None and data["lines"] is None


def test_a_file_with_nothing_hidden_in_it_has_empty_flags(uploader, kit):
    created = ok(upload(uploader, kit.docx()), 202)
    kit.run(kit.model())
    data = ok(uploader.get(f"{URL}/{created['id']}"))
    assert (data["hidden_text_chars"], data["hidden_text_flags"]) == (0, {"reasons": [], "samples": []})


def test_what_the_code_could_not_confirm_is_counted_apart_from_what_it_kept(uploader, kit):
    def a_wrong_employer(numbered):
        answer = kit.roles_answer(numbered)
        answer["items"][0]["employer"] = "Globex"
        return answer

    created = ok(upload(uploader, kit.docx()), 202)
    kit.run(kit.model({ROLES: a_wrong_employer}))
    data = ok(uploader.get(f"{URL}/{created['id']}"))
    assert data["facts"] == {"kept": 5, "unverified": 3, "to_review": 8}  # the role and its two achievements failed


def test_a_failed_document_says_why_in_a_code_and_in_words(uploader, kit):
    created = ok(upload(uploader, kit.pdf(["Mara"]), "scan.pdf"), 202)
    kit.run(kit.model())
    response = uploader.get(f"{URL}/{created['id']}")
    data = ok(response)
    assert (data["status"], data["parse_error_code"]) == ("failed", "scanned")
    assert "scanned image" in data["message"]
    assert (data["hidden_text_flags"], data["parsed_at"], data["facts"]["kept"]) == (None, None, 0)
    assert "Retry-After" not in response.headers


def test_a_partly_read_document_succeeds_and_says_part_of_it_could_not_be_read(uploader, kit):
    created = ok(upload(uploader, kit.docx()), 202)
    kit.run(kit.model({ROLES: SchemaInvalid()}))
    data = ok(uploader.get(f"{URL}/{created['id']}"))
    assert (data["status"], data["parse_error_code"]) == ("succeeded", "partial_extraction")
    assert "add what is gone" in data["message"] and data["facts"]["kept"] == 5


def test_upload_parse_and_review_end_to_end_with_the_source_lines(uploader, kit):
    created = ok(upload(uploader, kit.docx(hidden=kit.HIDDEN), "cv.docx"), 202)
    assert kit.run(kit.model()) == 1
    document = ok(uploader.get(f"{URL}/{created['id']}", params={"include": "text"}))
    text = document["text"]
    assert text == "\n".join(kit.RESUME) and kit.HIDDEN not in text
    assert [line["text"] for line in document["lines"]] == kit.RESUME
    assert [line["n"] for line in document["lines"]] == list(range(1, len(kit.RESUME) + 1))
    assert all(text[line["start"] : line["end"]] == line["text"] for line in document["lines"])

    facts = ok(uploader.get("/v1/me/facts", params={"status": "unconfirmed", "limit": 100}))
    assert len(facts) == kit.KEPT
    for fact in facts:  # each fact quotes the line it came from, at the span the database checked
        span = fact["provenance"]["span"]
        assert (fact["status"], fact["source"], fact["provenance"]["document_id"]) == (
            "unconfirmed",
            "resume",
            created["id"],
        )
        assert fact["statement"] == fact["provenance"]["quote"] == text[span["start"] : span["end"]]

    record = ok(uploader.get("/v1/me/record"))  # nothing is in the record until the candidate confirms it
    assert (record["facts"], record["unconfirmed_facts"]) == ([], kit.KEPT)
    assert [(d["id"], d["status"]) for d in record["documents"]] == [(created["id"], "succeeded")]

    review = {"decisions": [{"fact_id": f["id"], "action": "confirm"} for f in facts[:2]]}
    ok(uploader.post("/v1/me/facts/review", json=review, headers={"Idempotency-Key": uuid4().hex}))
    after = ok(uploader.get(f"{URL}/{created['id']}"))
    assert after["facts"] == {"kept": kit.KEPT, "unverified": 0, "to_review": kit.KEPT - 2}
    assert len(ok(uploader.get("/v1/me/record"))["facts"]) == 2


def test_another_persons_document_a_missing_one_and_a_bad_request_are_not_served(uploader, kit):
    theirs = kit.store(kit.person("bob@example.test"), kit.docx())
    refused(uploader.get(f"{URL}/{theirs}"), 404, "not_found")
    refused(uploader.get(f"{URL}/{theirs}", params={"include": "text"}), 404, "not_found")
    refused(uploader.get(f"{URL}/{uuid4()}"), 404, "not_found")
    refused(uploader.get(f"{URL}/not-an-id"), 422, "validation_failed")
    mine = ok(upload(uploader, kit.docx()), 202)
    refused(uploader.get(f"{URL}/{mine['id']}", params={"include": "everything"}), 422, "validation_failed")


def test_reading_a_status_needs_a_session(api_client, kit):
    refused(api_client.get(f"{URL}/{uuid4()}"), 401, "unauthenticated")
