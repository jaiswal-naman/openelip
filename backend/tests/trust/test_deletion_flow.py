"""Account deletion (TRU-02): the request with a fresh emailed code, the closing of the account, and the erase job.

The API opens its own connections as the real app_api role, so row-level security is in force. Most tests act as a
person through the session override; one signs in for real, with a code and a cookie, to prove the cookie goes. The
jobs run the way the worker runs them, through the queue, as the real app_worker role. The deep check that nothing is
left anywhere is the canary test; these prove the flow around it.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.platform import db, queue, relay
from app.trust import deletion

EMAIL = "ada@mail.test"


def delete(client, code: str, key: str | None = None):
    return client.post("/v1/privacy/deletion", json={"code": code}, headers={"Idempotency-Key": key or uuid4().hex})


def one(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def rows(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


def refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def accepted(response) -> dict:
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def wrong_code(right: str) -> str:
    return "000000" if right != "000000" else "111111"


def add_session(engine, user: UUID) -> None:
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO sessions (user_id, token_sha256, expires_at) VALUES (:u, :t, now() + interval '1 day')"),
            {"u": user, "t": uuid4().bytes * 2},
        )


def add_pending_mail(engine, user: UUID, key: str) -> None:
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO outbox (dedupe_key, user_id, template, payload) VALUES (:k, :u, 'brief_v1', '{}')"),
            {"k": key, "u": user},
        )


def request_of(engine, user: UUID):
    (row,) = rows(engine, "SELECT * FROM data_requests WHERE subject_user_id = :u AND kind = 'deletion'", u=user)
    return row


@pytest.fixture
def ada(make_person, sign_in_as):
    user = make_person(EMAIL)
    sign_in_as(user)
    return user


# ---- the request --------------------------------------------------------------------------------------


def test_a_fresh_code_closes_the_account_signs_the_person_out_and_queues_the_erasure(
    api_client, engine, make_person, emailed_code, ledger, mailbox
):
    """The whole path with a real session: sign in with a code, ask for another, delete."""
    assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": emailed_code(EMAIL)}).status_code == 201
    user = UUID(api_client.get("/v1/me").json()["data"]["id"])
    add_session(engine, user)  # a second device
    add_pending_mail(engine, user, "brief:1")
    bob = make_person("bob@mail.test")
    add_session(engine, bob)
    add_pending_mail(engine, bob, "brief:2")

    answer = accepted(delete(api_client, emailed_code(EMAIL)))

    assert (answer["kind"], answer["status"], answer["completed_at"], answer["download"]) == (
        "deletion",
        "queued",
        None,
        None,
    )
    assert answer["revoked"] == {"sessions": 2}  # the browser and the other device
    request = request_of(engine, user)
    assert (str(request.id), request.status, request.user_id) == (answer["id"], "open", user)
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=user) == "deleting"
    assert one(engine, "SELECT count(*) FROM sessions WHERE user_id = :u AND revoked_at IS NULL", u=user) == 0
    assert one(engine, "SELECT status FROM outbox WHERE dedupe_key = 'brief:1'") == "cancelled"  # nothing more is sent
    assert [tuple(j) for j in rows(engine, "SELECT kind, unique_key, payload FROM queue_jobs")] == [
        ("trust.erase", f"trust.erase:{request.id}", {"request_id": answer["id"]})
    ]
    # signed out at once: the old cookie no longer works
    refused(api_client.get("/v1/me"), 401, "unauthenticated")
    # and the account cannot be signed in to any more, even with a good code
    signin = api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": emailed_code(EMAIL)})
    refused(signin, 403, "forbidden")
    # somebody else's account, sessions and mail are untouched
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=bob) == "active"
    assert one(engine, "SELECT count(*) FROM sessions WHERE user_id = :u AND revoked_at IS NULL", u=bob) == 1
    assert one(engine, "SELECT status FROM outbox WHERE dedupe_key = 'brief:2'") == "pending"


def test_the_session_cookie_is_cleared_by_the_answer(api_client, emailed_code):
    assert api_client.post("/v1/auth/sessions", json={"email": EMAIL, "code": emailed_code(EMAIL)}).status_code == 201
    assert api_client.cookies.get("app_session")
    response = delete(api_client, emailed_code(EMAIL))
    assert response.status_code == 202
    assert 'app_session=""' in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"]
    assert not api_client.cookies.get("app_session")


def test_a_wrong_code_is_refused_counted_and_changes_nothing(ada, api_client, engine, emailed_code):
    add_session(engine, ada)
    add_pending_mail(engine, ada, "brief:1")
    right = emailed_code(EMAIL)

    error = refused(delete(api_client, wrong_code(right)), 401, "code_invalid")

    assert error["details"] == {"attempts_remaining": 4}
    assert one(engine, "SELECT attempts FROM login_codes WHERE lower(email) = :e", e=EMAIL) == 1  # kept: it was counted
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"
    assert one(engine, "SELECT count(*) FROM data_requests") == 0
    assert one(engine, "SELECT count(*) FROM queue_jobs") == 0
    assert one(engine, "SELECT count(*) FROM sessions WHERE revoked_at IS NOT NULL") == 0
    assert one(engine, "SELECT status FROM outbox WHERE dedupe_key = 'brief:1'") == "pending"
    assert one(engine, "SELECT consumed_at IS NULL FROM login_codes WHERE lower(email) = :e", e=EMAIL)  # still usable


def test_five_wrong_tries_lock_the_code_so_a_stolen_session_cannot_guess_for_ever(
    ada, api_client, engine, emailed_code
):
    right = emailed_code(EMAIL)
    for tried in range(1, 6):
        error = refused(delete(api_client, wrong_code(right)), 401, "code_invalid")
        assert error["details"]["attempts_remaining"] == 5 - tried
    refused(delete(api_client, right), 401, "code_invalid")  # too late: even the right code is spent
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"
    assert one(engine, "SELECT count(*) FROM data_requests") == 0


def test_without_a_code_having_been_asked_for_the_answer_is_the_same(ada, api_client, engine):
    error = refused(delete(api_client, "123456"), 401, "code_invalid")
    assert error["details"] == {"attempts_remaining": 0}
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"


def test_an_expired_code_is_refused(ada, api_client, engine, emailed_code):
    code = emailed_code(EMAIL)
    with engine.begin() as c:
        c.execute(text("UPDATE login_codes SET expires_at = now() - interval '1 second'"))
    refused(delete(api_client, code), 401, "code_invalid")
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"


def test_a_code_sent_to_somebody_elses_address_does_not_open_this_account(ada, api_client, engine, emailed_code):
    theirs = emailed_code("bob@mail.test")  # a valid code, but for another mailbox
    refused(delete(api_client, theirs), 401, "code_invalid")
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"
    assert one(engine, "SELECT attempts FROM login_codes WHERE lower(email) = 'bob@mail.test'") == 0  # theirs is intact


def test_the_code_is_used_up_by_a_deletion(ada, api_client, engine, emailed_code):
    accepted(delete(api_client, emailed_code(EMAIL)))
    assert one(engine, "SELECT consumed_at IS NOT NULL FROM login_codes WHERE lower(email) = :e", e=EMAIL)


def test_a_repeat_while_the_account_is_closing_returns_the_same_request_and_changes_nothing(
    ada, api_client, engine, emailed_code
):
    add_session(engine, ada)
    first = accepted(delete(api_client, emailed_code(EMAIL)))
    again = accepted(delete(api_client, "123456"))  # the session override still passes; no code is needed to see it
    assert again["id"] == first["id"] and again["revoked"] == {"sessions": 0}
    assert one(engine, "SELECT count(*) FROM data_requests WHERE kind = 'deletion'") == 1
    assert one(engine, "SELECT count(*) FROM queue_jobs") == 1


def test_two_requests_at_once_make_one_deletion(ada, engine, emailed_code):
    code = emailed_code(EMAIL)

    def ask(_: int):
        with db.transaction(ada) as conn:
            return deletion.request_deletion(conn, ada, code)

    with ThreadPoolExecutor(3) as pool:
        outcomes = list(pool.map(ask, range(3)))
    made = {o.request_id for o in outcomes if isinstance(o, deletion.Deletion)}
    assert len(made) == 1  # the others found it (or were told the code was spent), none made a second
    assert one(engine, "SELECT count(*) FROM data_requests WHERE kind = 'deletion'") == 1


@pytest.mark.parametrize("body", [{}, {"code": "12345"}, {"code": "1234567"}, {"code": "abcdef"}, {"code": 123456}])
def test_the_body_must_be_a_six_digit_code(ada, api_client, engine, body):
    response = api_client.post("/v1/privacy/deletion", json=body, headers={"Idempotency-Key": uuid4().hex})
    error = refused(response, 422, "validation_failed")
    assert [f["field"] for f in error["details"]["fields"]] == ["code"]
    assert one(engine, "SELECT status FROM users WHERE id = :u", u=ada) == "active"


def test_deletion_needs_a_key_and_json(ada, api_client):
    refused(api_client.post("/v1/privacy/deletion", json={"code": "123456"}), 400, "idempotency_key_missing")
    wrong = api_client.post(
        "/v1/privacy/deletion",
        content=b"code=123456",
        headers={"Idempotency-Key": uuid4().hex, "content-type": "text/plain"},
    )
    refused(wrong, 415, "unsupported_media_type")


def test_open_exports_end_as_failed_and_finished_ones_are_cleared_by_the_erasure(
    ada, api_client, engine, emailed_code, ledger, mailbox, run_jobs
):
    api_client.post("/v1/privacy/exports", json={}, headers={"Idempotency-Key": uuid4().hex})
    assert run_jobs() == 1  # one export is finished ...
    api_client.post("/v1/privacy/exports", json={}, headers={"Idempotency-Key": uuid4().hex})  # ... and one is waiting
    done, waiting = (
        r.id for r in rows(engine, "SELECT id FROM data_requests WHERE kind = 'export' ORDER BY created_at")
    )
    assert one(engine, "SELECT export_archive IS NOT NULL FROM data_requests WHERE id = :r", r=done)

    accepted(delete(api_client, emailed_code(EMAIL)))
    assert one(engine, "SELECT status FROM data_requests WHERE id = :r", r=waiting) == "failed"  # no copy is made now

    assert run_jobs() == 2  # the waiting export job ends at once, the erasure runs
    assert one(engine, "SELECT export_archive IS NULL FROM data_requests WHERE id = :r", r=done)  # the copy is gone
    kept = rows(engine, "SELECT id, subject_user_id, user_id FROM data_requests ORDER BY created_at")
    assert [(r.subject_user_id, r.user_id) for r in kept] == [(ada, None)] * 3  # bare id only, no link to a person


# ---- the erase job ------------------------------------------------------------------------------------


def start_deletion(api_client, emailed_code) -> UUID:
    return UUID(accepted(delete(api_client, emailed_code(EMAIL)))["id"])


def test_the_erase_job_erases_the_person_writes_the_ledger_and_tells_them_keeping_nothing(
    ada, api_client, engine, make_person, emailed_code, ledger, mailbox, run_jobs, caplog
):
    add_session(engine, ada)
    bob = make_person("bob@mail.test")
    request_id = start_deletion(api_client, emailed_code)
    with caplog.at_level(logging.INFO, logger="app.event"):
        assert run_jobs() == 1

    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=ada) == 0
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=bob) == 1
    request = rows(engine, "SELECT * FROM data_requests WHERE id = :r", r=request_id)[0]
    assert (request.status, request.user_id, request.subject_user_id) == ("completed", None, ada)
    assert request.store_results["postgres"] == "deleted" and request.completed_at is not None
    event = rows(engine, "SELECT * FROM audit_log WHERE event_type = 'user.erased'")[0]
    assert (event.entity_table, event.entity_id, event.subject_user_id) == ("data_requests", str(request_id), None)

    # the ledger: one line, the bare id and the time
    (line,) = ledger.read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert set(entry) == {"user_id", "deleted_at"} and entry["user_id"] == str(ada)
    assert abs((request.completed_at - datetime.fromisoformat(entry["deleted_at"])).total_seconds()) < 60

    # one message, to the address, after the erasure
    (message,) = mailbox
    assert message["To"] == EMAIL and "deleted" in message["Subject"]
    body = message.get_content()
    assert EMAIL not in body and str(ada) not in body  # it names nobody
    # and the address was not kept anywhere to send it from: not in a row, not in a log line
    for table in ("outbox", "queue_jobs", "audit_log", "data_requests", "idempotency_keys", "login_codes", "users"):
        assert EMAIL not in one(engine, f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"), table
    assert EMAIL not in caplog.text and str(ada) not in caplog.text  # the logs name neither the address nor the person
    job = rows(engine, "SELECT status, payload FROM queue_jobs WHERE kind = 'trust.erase'")[0]
    assert (job.status, job.payload) == (
        "succeeded",
        {"request_id": str(request_id)},
    )  # the job kept only the request id


def test_the_erase_job_is_safe_to_run_twice(ada, api_client, engine, emailed_code, ledger, mailbox, run_jobs):
    request_id = start_deletion(api_client, emailed_code)
    assert run_jobs() == 1
    with db.transaction(role="worker") as conn:
        deletion.erase(conn, request_id)  # a second delivery of the same job
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1 and len(mailbox) == 1


def test_a_failed_message_does_not_undo_or_fail_the_erasure(
    ada, api_client, engine, emailed_code, ledger, monkeypatch, run_jobs
):
    def refuse(settings, message):
        raise RuntimeError("the mail server is on fire")

    monkeypatch.setattr(relay, "_send", refuse)
    start_deletion(api_client, emailed_code)
    assert run_jobs() == 1
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=ada) == 0
    assert one(engine, "SELECT status FROM queue_jobs WHERE kind = 'trust.erase'") == "succeeded"


@pytest.mark.parametrize("address", ["someone@example.test", "someone@sub.example.test"])
def test_nothing_is_sent_to_an_address_that_cannot_exist(
    api_client, engine, make_person, sign_in_as, emailed_code, ledger, mailbox, run_jobs, address
):
    user = make_person(address)
    sign_in_as(user)
    accepted(delete(api_client, emailed_code(address)))
    assert run_jobs() == 1
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=user) == 0 and mailbox == []  # erased, not mailed


def test_a_synthetic_account_is_erased_and_never_mailed(
    api_client, engine, make_person, sign_in_as, emailed_code, ledger, mailbox, run_jobs
):
    user = make_person("demo@mail.test")
    with engine.begin() as c:
        c.execute(text("UPDATE users SET is_synthetic = true WHERE id = :u"), {"u": user})
    sign_in_as(user)
    accepted(delete(api_client, emailed_code("demo@mail.test")))
    assert run_jobs() == 1
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=user) == 0 and mailbox == []


def test_if_the_ledger_cannot_be_written_nothing_is_erased_and_the_job_runs_again(
    ada, api_client, engine, emailed_code, mailbox, run_jobs, tmp_path, monkeypatch
):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("a file where the ledger's folder should be", encoding="utf-8")
    monkeypatch.setenv("ERASURE_LEDGER_PATH", str(blocker / "ledger.jsonl"))
    add_session(engine, ada)
    request_id = start_deletion(api_client, emailed_code)

    assert run_jobs() == 1
    # nothing was erased: the person is still closed, their rows are all there, no message went out
    assert (one(engine, "SELECT status FROM users WHERE id = :u", u=ada), mailbox) == ("deleting", [])
    assert one(engine, "SELECT count(*) FROM sessions WHERE user_id = :u", u=ada) == 1
    assert one(engine, "SELECT status FROM data_requests WHERE id = :r", r=request_id) == "open"
    assert tuple(rows(engine, "SELECT status, last_error_code FROM queue_jobs")[0]) == ("queued", "FileExistsError")

    ledger = tmp_path / "volume" / "ledger.jsonl"  # the operator fixes the volume; the retry then goes through
    monkeypatch.setenv("ERASURE_LEDGER_PATH", str(ledger))
    with engine.begin() as c:
        c.execute(text("UPDATE queue_jobs SET run_at = now()"))
    assert run_jobs() == 1
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=ada) == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["user_id"] == str(ada)
    assert len(mailbox) == 1


def test_the_erase_job_takes_the_person_from_the_request_never_from_the_payload(
    ada, api_client, engine, make_person, ledger, mailbox, run_jobs
):
    bob = make_person("bob@mail.test")
    with engine.begin() as c:
        export = c.execute(
            text(
                """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
                   VALUES (:u, :u, 'export', now() + interval '1 day') RETURNING id"""
            ),
            {"u": ada},
        ).scalar_one()
        for number, payload in enumerate(
            [{"request_id": str(export)}, {"request_id": str(uuid4()), "user_id": str(bob)}, {"user_id": str(bob)}]
        ):
            queue.enqueue(c, "trust.erase", payload, unique_key=f"trust.erase:bad-{number}")
    assert run_jobs() == 3
    states = rows(engine, "SELECT status, attempts, last_error_code FROM queue_jobs ORDER BY id")
    assert [tuple(s) for s in states] == [
        ("dead", 1, "NonRetryable")
    ] * 3  # an export, nothing, no request: all refused
    assert one(engine, "SELECT count(*) FROM users") == 2 and not ledger.exists() and mailbox == []


def test_only_the_worker_can_erase(ada, engine):
    with db.transaction(ada) as conn:
        request_id = conn.execute(
            text(
                """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
                   VALUES (:u, :u, 'deletion', now() + interval '1 day') RETURNING id"""
            ),
            {"u": ada},
        ).scalar_one()
    with pytest.raises(DBAPIError), db.transaction(ada) as conn:  # the API role: no EXECUTE on erase_user
        conn.execute(text("SELECT erase_user(:u, :r)"), {"u": ada, "r": request_id})
    assert one(engine, "SELECT count(*) FROM users WHERE id = :u", u=ada) == 1


def test_the_ledger_path_defaults_to_the_volume_and_follows_the_environment(monkeypatch):
    monkeypatch.delenv("ERASURE_LEDGER_PATH", raising=False)
    assert str(deletion.ledger_path()).replace("\\", "/") == "/srv/erasure/ledger.jsonl"
    monkeypatch.setenv("ERASURE_LEDGER_PATH", "somewhere/else.jsonl")
    assert deletion.ledger_path().as_posix() == "somewhere/else.jsonl"
