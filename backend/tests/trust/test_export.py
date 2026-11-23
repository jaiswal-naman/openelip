"""The data export (TRU-01): the request, the job that builds the archive, and who may download it, for how long.

The API opens its own connections as the real app_api role, so row-level security is in force; what a request commits
stays. The tests use api_client (which empties the tables around each test) and read what was stored through the
bootstrap engine. The jobs run the way the worker runs them, through the queue.
"""

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.platform import db, queue, scheduler, sessions
from app.profile import documents
from app.trust import api, export, jobs, privacy
from app.trust.routes import API_STATUS

FORMAT_MODULES = {"brief", "evals", "hiring", "match", "platform", "profile", "trust"}


class Seeded:
    def __init__(self, user: UUID, email: str, tag: str, pdf: bytes, document: UUID) -> None:
        self.user, self.email, self.tag, self.pdf, self.document = user, email, tag, pdf, document


def seed(engine, email: str, tag: str) -> Seeded:
    """One person with a row in every table the export reads, every text field carrying `tag`."""
    pdf = f"%PDF-1.7 invented resume {tag}".encode()
    statement = f"Staff engineer at Initech {tag}"
    with engine.begin() as c:
        user = c.execute(
            text("INSERT INTO users (email, display_name) VALUES (:e, :n) RETURNING id"),
            {"e": email, "n": f"Name {tag}"},
        ).scalar_one()
        c.execute(
            text("INSERT INTO sessions (user_id, token_sha256, expires_at) VALUES (:u, :t, now() + interval '1 day')"),
            {"u": user, "t": hashlib.sha256(tag.encode()).digest()},
        )
        c.execute(
            text("INSERT INTO outbox (dedupe_key, user_id, template, payload) VALUES (:k, :u, 'brief_v1', :p)"),
            {"k": f"brief:{tag}", "u": user, "p": json.dumps({"note": tag})},
        )
        model = c.execute(
            text(
                """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
                   VALUES ('local', :m, 'chat', true, 'test') RETURNING id"""
            ),
            {"m": f"model-{tag}"},
        ).scalar_one()
        c.execute(
            text(
                """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, latency_ms)
                   VALUES ('ping', 'v1', :m, 'ok', :u, 5)"""
            ),
            {"m": model, "u": user},
        )
        document = c.execute(
            text(
                """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                          extracted_text, parsed_at)
                   VALUES (:u, :c, :n, 'application/pdf', :s, :h, 'parsed', :t, now()) RETURNING id"""
            ),
            {
                "u": user,
                "c": sessions.seal_bytes(pdf),
                "n": f"resume {tag}.pdf",
                "s": len(pdf),
                "h": hashlib.sha256(pdf).digest(),
                "t": statement,
            },
        ).scalar_one()
        c.execute(
            text(
                """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
                   VALUES (:u, 'role', :t, 'resume', :d, int4range(0, :n), :t)"""
            ),
            {"u": user, "t": statement, "d": document, "n": len(statement)},
        )
        api.grant_consent(c, user, "terms", "terms-v1")
    return Seeded(user, email, tag, pdf, document)


@pytest.fixture
def ada(engine, sign_in_as):
    person = seed(engine, "ada@mail.test", "ADACANARY")
    sign_in_as(person.user)
    return person


def post(client, path: str, key: str | None = None, **kwargs):
    return client.post(path, headers={"Idempotency-Key": key or uuid4().hex}, **kwargs)


def ask(client, key: str | None = None):
    return post(client, "/v1/privacy/exports", key, json={})


def data(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def request_row(engine, request_id):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM data_requests WHERE id = :r"), {"r": request_id}).one()


def stored_archive(engine, request_id) -> bytes:
    """The archive as it was built, opened from its seal."""
    return sessions.open_sealed_bytes(bytes(request_row(engine, request_id).export_archive))


def open_zip(archive: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(archive))


def build(client, engine, run_jobs) -> UUID:
    """Ask for an export and let the worker make it; returns the request id."""
    request_id = UUID(data(ask(client), 202)["id"])
    assert run_jobs() == 1
    return request_id


# ---- asking -------------------------------------------------------------------------------------------


def test_an_export_is_requested_answered_with_202_and_a_location_and_queued_once(ada, api_client, engine):
    response = ask(api_client)
    answer = data(response, 202)
    request_id = UUID(answer["id"])
    assert response.headers["Location"] == f"/v1/privacy/requests/{request_id}"
    assert (answer["kind"], answer["status"], answer["completed_at"], answer["download"]) == (
        "export",
        "queued",
        None,
        None,
    )
    assert answer["format"] == export.FORMAT
    row = request_row(engine, request_id)
    assert (row.kind, row.status, row.user_id, row.subject_user_id) == ("export", "open", ada.user, ada.user)
    assert timedelta(hours=23) < row.due_at - row.created_at < timedelta(hours=25)  # due in a day
    with engine.connect() as c:
        jobs_queued = c.execute(text("SELECT kind, unique_key, payload FROM queue_jobs")).all()
    assert [tuple(job) for job in jobs_queued] == [
        ("trust.export", f"trust.export:{request_id}", {"request_id": str(request_id)})
    ]


def test_asking_again_while_one_is_open_gives_that_one_and_queues_nothing_more(ada, api_client, engine):
    first = data(ask(api_client), 202)
    again = data(ask(api_client), 202)  # another key, so this is a new request that finds the open one
    assert again["id"] == first["id"]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_requests WHERE kind = 'export'")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM queue_jobs")).scalar_one() == 1


def test_a_repeat_with_the_same_key_gets_the_same_answer_and_the_same_location(ada, api_client):
    first = ask(api_client, "key-for-one-export")
    replay = ask(api_client, "key-for-one-export")
    assert replay.status_code == 202 and replay.json()["meta"]["idempotent_replay"] is True
    assert replay.json()["data"] == first.json()["data"]
    assert replay.headers["Location"] == first.headers["Location"]


def test_an_open_request_past_its_due_time_does_not_block_a_new_export(ada, api_client, engine):
    stuck = UUID(data(ask(api_client), 202)["id"])
    with engine.begin() as c:  # the job died for good and nobody noticed for a day
        c.execute(text("UPDATE data_requests SET due_at = now() - interval '1 minute' WHERE id = :r"), {"r": stuck})
    fresh = UUID(data(ask(api_client), 202)["id"])
    assert fresh != stuck


def test_the_third_export_within_a_day_is_refused_with_the_wait(ada, api_client, engine, run_jobs):
    build(api_client, engine, run_jobs)
    build(api_client, engine, run_jobs)
    response = ask(api_client)
    error = refused(response, 429, "rate_limited")
    assert 1 <= error["details"]["retry_after_seconds"] <= 86400
    assert response.headers["Retry-After"] == str(error["details"]["retry_after_seconds"])  # API.md 1.8
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM data_requests WHERE kind = 'export'")).scalar_one() == 2


def test_two_requests_at_the_same_moment_make_one(ada, engine):
    from concurrent.futures import ThreadPoolExecutor

    def ask_in_a_transaction(_: int) -> UUID:
        with db.transaction(ada.user) as conn:
            return export.request_export(conn, ada.user)

    with ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(ask_in_a_transaction, range(4)))
    assert len(set(ids)) == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/privacy/exports"),
        ("get", f"/v1/privacy/requests/{uuid4()}"),
        ("get", f"/v1/privacy/requests/{uuid4()}/download"),
        ("post", "/v1/privacy/deletion"),
    ],
)
def test_every_privacy_route_that_acts_for_a_person_needs_a_session(api_client, method, path):
    refused(getattr(api_client, method)(path, headers={"Idempotency-Key": uuid4().hex}), 401, "unauthenticated")


def test_asking_needs_an_idempotency_key_and_json(ada, api_client):
    refused(api_client.post("/v1/privacy/exports", json={}), 400, "idempotency_key_missing")
    wrong = api_client.post(
        "/v1/privacy/exports", content=b"x=1", headers={"Idempotency-Key": uuid4().hex, "content-type": "text/plain"}
    )
    refused(wrong, 415, "unsupported_media_type")


# ---- building -----------------------------------------------------------------------------------------


def test_the_job_writes_one_zip_with_a_manifest_a_json_file_per_module_and_the_original_files(
    ada, api_client, engine, run_jobs
):
    request_id = build(api_client, engine, run_jobs)
    archive = open_zip(stored_archive(engine, request_id))
    manifest = json.loads(archive.read("manifest.json"))

    path = f"files/documents/{ada.document}-resume_{ada.tag}.pdf"
    assert set(archive.namelist()) == {"manifest.json", *(f"{m}.json" for m in FORMAT_MODULES), path}
    assert archive.read(path) == ada.pdf  # the original file, as uploaded, not its sealed form
    assert manifest["format_version"] == export.FORMAT_VERSION
    datetime.fromisoformat(manifest["created_at"])
    assert set(manifest["modules"]) == FORMAT_MODULES
    assert manifest["modules"]["profile"] == {
        "file": "profile.json",
        "rows": {"documents": 1, "profile_events": 0, "profile_facts": 1, "constraints": 0, "preferences": 0},
    }
    # the manifest lists every file but itself, with its size
    listed = {item["path"]: item["bytes"] for item in manifest["files"]}
    assert listed == {name: archive.getinfo(name).file_size for name in archive.namelist() if name != "manifest.json"}
    profile = json.loads(archive.read("profile.json"))
    assert profile["profile_facts"][0]["statement"] == f"Staff engineer at Initech {ada.tag}"
    assert profile["documents"][0]["sha256"] == hashlib.sha256(ada.pdf).hexdigest()
    assert "content" not in profile["documents"][0]


def test_the_account_is_in_the_archive_without_credential_hashes(ada, api_client, engine, run_jobs):
    archive = open_zip(stored_archive(engine, build(api_client, engine, run_jobs)))
    platform = json.loads(archive.read("platform.json"))
    assert set(platform) == {"users", "sessions", "outbox", "queue_jobs", "idempotency_keys", "llm_calls"}
    (user,) = platform["users"]
    assert (user["email"], user["display_name"], user["record_embedding"]) == (ada.email, f"Name {ada.tag}", None)
    (session,) = platform["sessions"]
    assert "token_sha256" not in session and session["user_id"] == str(ada.user)
    assert [row["template"] for row in platform["outbox"]] == ["brief_v1"]
    assert [row["task"] for row in platform["llm_calls"]] == ["ping"]
    # the idempotency record of the very request that asked for this export
    assert [row["path"] for row in platform["idempotency_keys"]] == ["/v1/privacy/exports"]
    assert all("request_sha256" not in row for row in platform["idempotency_keys"])


def test_the_export_job_and_the_parse_of_an_upload_belong_to_the_person_and_the_archive_lists_them(
    ada, api_client, engine, run_jobs
):
    """queue.enqueue is given the person, so the archive's queue_jobs holds their jobs and no one else's, and the
    erasure (a cascade on that column) reaches them."""
    bob = seed(engine, "bob@mail.test", "BOBCANARY")
    for person in (ada, bob):
        with db.transaction(person.user) as conn:
            resume = b"%PDF-1.7 invented resume " + person.tag.encode()
            documents.store_document(conn, person.user, "resume.pdf", resume)
    with engine.begin() as c:  # the parses wait a day, so the one job run below is the export
        c.execute(text("UPDATE queue_jobs SET run_at = now() + interval '1 day' WHERE kind = 'profile.parse'"))
    request_id = build(api_client, engine, run_jobs)

    with engine.connect() as c:
        owners = {(row.kind, row.user_id) for row in c.execute(text("SELECT kind, user_id FROM queue_jobs"))}
    assert owners == {("profile.parse", ada.user), ("profile.parse", bob.user), ("trust.export", ada.user)}
    platform = json.loads(open_zip(stored_archive(engine, request_id)).read("platform.json"))
    assert sorted(row["kind"] for row in platform["queue_jobs"]) == ["profile.parse", "trust.export"]
    assert {row["user_id"] for row in platform["queue_jobs"]} == {str(ada.user)}


def test_an_embedding_is_exported_as_numbers(ada, api_client, engine, run_jobs):
    vector = "[" + ",".join(["0.25"] * 384) + "]"
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE users SET record_embedding = CAST(:v AS vector), record_embedding_model = 'test' WHERE id = :u"
            ),
            {"v": vector, "u": ada.user},
        )
    archive = open_zip(stored_archive(engine, build(api_client, engine, run_jobs)))
    (user,) = json.loads(archive.read("platform.json"))["users"]
    assert user["record_embedding"] == [0.25] * 384 and user["record_embedding_model"] == "test"


def test_the_archive_holds_the_trust_rows_of_the_person_and_no_archive_inside_itself(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    trust = json.loads(open_zip(stored_archive(engine, request_id)).read("trust.json"))
    assert [row["purpose"] for row in trust["consents"]] == ["terms"]
    (request,) = trust["data_requests"]
    assert request["id"] == str(request_id) and "export_archive" not in request


def test_the_archive_holds_the_requesting_person_and_nobody_else(ada, api_client, engine, run_jobs):
    bob = seed(engine, "bob@mail.test", "BOBCANARY")
    archive = open_zip(stored_archive(engine, build(api_client, engine, run_jobs)))
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert ada.tag.encode() in everything
    for other in (bob.tag, str(bob.user), bob.email, str(bob.document)):
        assert other.encode() not in everything
    assert not [name for name in archive.namelist() if str(bob.document) in name]


def test_the_archive_is_stored_sealed_and_kept_for_seven_days(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    row = request_row(engine, request_id)
    raw = bytes(row.export_archive)
    assert not raw.startswith(b"PK") and ada.tag.encode() not in raw and ada.pdf not in raw  # nothing readable at rest
    assert sessions.open_sealed_bytes(raw).startswith(b"PK")  # it is a ZIP once opened with the data keys
    assert (row.status, row.completed_at is not None) == ("completed", True)
    assert timedelta(days=6, hours=23) < row.export_expires_at - row.completed_at < timedelta(days=7, hours=1)
    assert row.store_results == {"postgres": "exported", "modules": len(FORMAT_MODULES), "files": 1}


def test_the_job_leaves_one_audit_row_of_ids_and_counts_and_nothing_else(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    with engine.connect() as c:
        rows = c.execute(text("SELECT * FROM audit_log WHERE event_type = 'privacy.export_ready'")).all()
    (event,) = rows
    assert (event.actor_type, event.actor_user_id, event.subject_user_id) == ("system", None, ada.user)
    assert (event.entity_table, event.entity_id) == ("data_requests", str(request_id))
    assert event.details == {"modules": len(FORMAT_MODULES), "files": 1}


def test_the_job_is_safe_to_run_twice(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    before = request_row(engine, request_id)
    with db.transaction(role="worker") as conn:
        export.complete_export(conn, request_id)  # a second delivery of the same job
    after = request_row(engine, request_id)
    assert bytes(after.export_archive) == bytes(before.export_archive) and after.completed_at == before.completed_at
    with engine.connect() as c:
        assert (
            c.execute(text("SELECT count(*) FROM audit_log WHERE event_type = 'privacy.export_ready'")).scalar_one()
            == 1
        )


def test_a_job_for_a_request_that_does_not_exist_or_is_not_an_export_is_dead_at_once(engine, ada, api_client, run_jobs):
    with engine.begin() as c:
        deletion = c.execute(
            text(
                """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
                   VALUES (:u, :u, 'deletion', now() + interval '1 day') RETURNING id"""
            ),
            {"u": ada.user},
        ).scalar_one()
        for number, request_id in enumerate((uuid4(), deletion)):
            queue.enqueue(c, "trust.export", {"request_id": str(request_id)}, unique_key=f"trust.export:wrong-{number}")
        queue.enqueue(c, "trust.export", {"nothing": "here"}, unique_key="trust.export:wrong-payload")
    assert run_jobs() == 3
    with engine.connect() as c:
        states = c.execute(text("SELECT status, attempts, last_error_code FROM queue_jobs ORDER BY id")).all()
    assert [tuple(s) for s in states] == [("dead", 1, "NonRetryable")] * 3  # nothing to retry: a retry cannot help


def test_an_account_that_is_closing_gets_no_archive_and_its_request_ends_as_failed(ada, api_client, engine, run_jobs):
    request_id = UUID(data(ask(api_client), 202)["id"])
    with engine.begin() as c:
        c.execute(text("UPDATE users SET status = 'deleting' WHERE id = :u"), {"u": ada.user})
    assert run_jobs() == 1
    row = request_row(engine, request_id)
    assert (row.status, row.export_archive, row.completed_at) == ("failed", None, None)


def test_a_failing_exporter_fails_the_job_and_leaves_the_request_open_to_retry(ada, api_client, engine, monkeypatch):
    def broken(conn, user_id):
        raise RuntimeError("canary text that must not be stored")

    monkeypatch.setitem(privacy._exporters, "trust", broken)
    request_id = UUID(data(ask(api_client), 202)["id"])
    assert queue.run_once(lambda: db.transaction(role="worker"), "w") == 1
    with engine.connect() as c:
        job = c.execute(text("SELECT status, last_error_code FROM queue_jobs")).one()
    assert tuple(job) == ("queued", "RuntimeError")  # the class name only, never the message
    assert request_row(engine, request_id).status == "open" and request_row(engine, request_id).export_archive is None


# ---- what goes into the archive -----------------------------------------------------------------------


def make_exporter(**files):
    return lambda conn, user_id: privacy.ModuleExport(data={"things": [1, 2]}, files=files)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "files/../outside.txt",
        "/etc/passwd",
        "files//double.txt",
        "files/a\\b.txt",
        "documents/not-under-files.pdf",
        "files/",
        "files/with space.txt",
        "manifest.json",
    ],
)
def test_a_file_path_that_could_leave_the_archive_is_refused_naming_only_the_module(conn, monkeypatch, path):
    monkeypatch.setattr(privacy, "_exporters", {"plain": make_exporter(**{path: b"x"})})
    with pytest.raises(ValueError) as refusal:
        export.build_export(conn, uuid4())
    assert "plain" in str(refusal.value) and path not in str(refusal.value)


def test_two_modules_may_not_write_the_same_path_and_a_module_name_must_be_plain(conn, monkeypatch):
    monkeypatch.setattr(
        privacy,
        "_exporters",
        {"one": make_exporter(**{"files/x.txt": b"1"}), "two": make_exporter(**{"files/x.txt": b"2"})},
    )
    with pytest.raises(ValueError, match="two: an archive path is already taken"):
        export.build_export(conn, uuid4())
    monkeypatch.setattr(privacy, "_exporters", {"manifest": make_exporter()})  # would overwrite manifest.json
    with pytest.raises(ValueError, match="already taken"):
        export.build_export(conn, uuid4())
    monkeypatch.setattr(privacy, "_exporters", {"../up": make_exporter()})
    with pytest.raises(ValueError, match="plain identifier"):
        export.build_export(conn, uuid4())


def test_a_new_module_with_an_exporter_is_in_the_archive_with_no_change_to_the_export(conn, monkeypatch):
    monkeypatch.setattr(privacy, "_exporters", {"campaigns": make_exporter(**{"files/campaigns/notes.txt": b"hello"})})
    built = export.build_export(conn, uuid4())
    archive = open_zip(built.archive)
    assert sorted(archive.namelist()) == ["campaigns.json", "files/campaigns/notes.txt", "manifest.json"]
    assert (built.rows, built.files) == ({"campaigns": {"things": 2}}, 1)


# Registered tables that are deliberately not in the export, each with the reason.
NOT_EXPORTED = {
    "login_codes": "no foreign key: sign-in codes are kept as keyed hashes for a day and describe no one",
    # The hiring module registers them set_null, because the ownership test refuses "no personal data" for a table
    # that points at users; what they hold is the employer's own record, which DATA_MODEL 7.3 leaves out of an export.
    "organizations": "an employer's own record: erasure only clears created_by, and 7.3 excludes employer-side data",
    "roles": "an employer's posting, not the person's data: erasure only clears created_by (7.3 excludes it)",
}


def test_every_registered_table_that_can_hold_a_person_is_exported_or_has_a_reason_not_to(conn):
    exported = {table for module in privacy.exporters().values() for table in module(conn, uuid4()).data}
    missing = {
        table: effect
        for tables in privacy.registry().values()
        for table, effect in tables.items()
        if effect != "no_personal_data" and table not in exported and table not in NOT_EXPORTED
    }
    assert not missing, (
        f"{sorted(missing)} hold data about a person but no exporter returns them. Add them to the exporter of "
        "the module that registered them, or to NOT_EXPORTED here with the reason."
    )


# ---- reading it back ----------------------------------------------------------------------------------


def test_the_status_goes_from_queued_to_succeeded_and_then_offers_the_download(ada, api_client, engine, run_jobs):
    request_id = UUID(data(ask(api_client), 202)["id"])
    waiting = api_client.get(f"/v1/privacy/requests/{request_id}")
    assert data(waiting)["status"] == "queued" and data(waiting)["download"] is None
    assert waiting.headers["Retry-After"] == "5"  # still being made: the client should poll again

    run_jobs()
    done = api_client.get(f"/v1/privacy/requests/{request_id}")
    answer = data(done)
    assert "Retry-After" not in done.headers
    assert (answer["status"], answer["kind"]) == ("succeeded", "export")
    assert answer["download"]["method"] == "GET"
    assert answer["download"]["path"] == f"/v1/privacy/requests/{request_id}/download"
    expires = request_row(engine, request_id).export_expires_at
    assert datetime.fromisoformat(answer["download"]["available_until"]) == expires
    assert answer["completed_at"] is not None


def test_the_api_status_words_map_every_status_the_table_has():
    assert set(API_STATUS) == {"open", "in_progress", "completed", "failed"}  # DATA_MODEL data_request_status


def test_the_download_is_a_zip_with_the_original_file_and_is_never_cached(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    response = api_client.get(f"/v1/privacy/requests/{request_id}/download")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="data-export-')
    assert response.content == stored_archive(engine, request_id)  # what the person gets is what was built
    assert open_zip(response.content).read(f"files/documents/{ada.document}-resume_{ada.tag}.pdf") == ada.pdf


def test_the_download_waits_409_until_it_is_ready_and_says_in_what_state(ada, api_client, engine, run_jobs):
    request_id = UUID(data(ask(api_client), 202)["id"])
    error = refused(api_client.get(f"/v1/privacy/requests/{request_id}/download"), 409, "conflict")
    assert error["details"] == {"state": "queued"}


def test_another_person_cannot_see_or_download_the_request(ada, api_client, engine, run_jobs, sign_in_as):
    request_id = build(api_client, engine, run_jobs)
    bob = seed(engine, "bob@mail.test", "BOBCANARY")
    sign_in_as(bob.user)
    refused(api_client.get(f"/v1/privacy/requests/{request_id}"), 404, "not_found")
    refused(api_client.get(f"/v1/privacy/requests/{request_id}/download"), 404, "not_found")
    refused(
        api_client.get(f"/v1/privacy/requests/{uuid4()}"), 404, "not_found"
    )  # and one that never existed is the same
    sign_in_as(ada.user)
    assert api_client.get(f"/v1/privacy/requests/{request_id}/download").status_code == 200


def test_a_deletion_request_is_not_a_download(ada, api_client, engine):
    with engine.begin() as c:
        deletion = c.execute(
            text(
                """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
                   VALUES (:u, :u, 'deletion', now() + interval '1 day') RETURNING id"""
            ),
            {"u": ada.user},
        ).scalar_one()
    refused(api_client.get(f"/v1/privacy/requests/{deletion}/download"), 404, "not_found")
    assert data(api_client.get(f"/v1/privacy/requests/{deletion}"))["kind"] == "deletion"  # its status can be read


def test_after_its_expiry_the_download_is_gone_410_and_the_status_offers_none(ada, api_client, engine, run_jobs):
    request_id = build(api_client, engine, run_jobs)
    with engine.begin() as c:
        c.execute(
            text("UPDATE data_requests SET export_expires_at = now() - interval '1 second' WHERE id = :r"),
            {"r": request_id},
        )
    error = refused(api_client.get(f"/v1/privacy/requests/{request_id}/download"), 410, "not_found")
    assert error["details"] == {"state": "expired"}
    answer = data(api_client.get(f"/v1/privacy/requests/{request_id}"))
    assert (answer["status"], answer["download"]) == ("succeeded", None)


def test_the_daily_sweep_clears_an_expired_archive_and_leaves_a_fresh_one(ada, api_client, engine, run_jobs):
    stale, fresh = build(api_client, engine, run_jobs), build(api_client, engine, run_jobs)
    with engine.begin() as c:
        c.execute(
            text("UPDATE data_requests SET export_expires_at = now() - interval '1 hour' WHERE id = :r"), {"r": stale}
        )
        queue.enqueue(c, "trust.purge", {}, unique_key="trust.purge:test")
    assert run_jobs() == 1
    assert request_row(engine, stale).export_archive is None
    assert request_row(engine, fresh).export_archive is not None
    refused(api_client.get(f"/v1/privacy/requests/{stale}/download"), 410, "not_found")


def test_the_sweep_is_scheduled_once_a_day_with_the_workers_other_upkeep():
    assert scheduler._every["trust.purge"] == 86400
    assert queue._handlers["trust.purge"] is jobs.purge
    assert queue._handlers["trust.export"] is jobs.export_job and queue._handlers["trust.erase"] is jobs.erase_job
