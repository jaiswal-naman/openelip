"""TWN-01, TWN-03 and TWN-04 over HTTP (plan 09-03): /v1/twin/scopes and /v1/twin/questions as the real app_api role.
The worker's half runs through the queue as app_worker, as the worker process would. Everything is invented."""

import threading
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.guardian import jobs  # noqa: F401  (registers guardian.release)
from app.platform import db
from app.platform.sessions import Principal, current_user
from app.twin import api as twin
from app.twin import logic
from tests.hiring.factories import make_member, make_org
from tests.twin.factories import withdraw
from tests.twin.fixtures.graph import world as w

TOPIC_ORDER = [
    "experience", "skills", "education", "projects", "availability", "work_authorization", "location",
    "expected_pay", "motivation", "other", "current_pay", "contact_details",
]  # fmt: skip


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


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


def sign_in(client, user) -> None:
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


@pytest.fixture
def no_wait(monkeypatch):
    """The ask does not wait for the worker (the state comes back `pending`)."""
    original = twin.wait
    monkeypatch.setattr(twin, "wait", lambda question_id, read, seconds=0.0: original(question_id, read, 0.0))


@pytest.fixture
def world(api_client, engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    return w.build(engine, dial="tell_me_after", topics={"experience": "answer"})


def post(client, world, question="What did you build at your last job?", **extra):
    return client.post(
        "/v1/twin/questions",
        json={"introduction_id": str(world.world.introduction), "question": question},
        headers=key(),
        **extra,
    )


# ---- scopes ----------------------------------------------------------------------------------------------------------


def test_the_scopes_list_the_twelve_topics_in_order_with_their_defaults(api_client, world):
    sign_in(api_client, world.candidate)
    # the fixture's own rows are replaced by an empty list first: no row means ask_me
    ok(api_client.put("/v1/twin/scopes", json={"topics": []}))
    topics = ok(api_client.get("/v1/twin/scopes"))["topics"]
    assert [t["topic"] for t in topics] == TOPIC_ORDER
    by = {t["topic"]: t for t in topics}
    assert by["experience"] == {"topic": "experience", "permission": "ask_me", "locked": False}
    assert by["current_pay"] == {"topic": "current_pay", "permission": "never", "locked": True}
    assert by["contact_details"]["locked"] is True and by["contact_details"]["permission"] == "never"


def test_put_replaces_the_rows_and_a_topic_left_out_goes_back_to_ask_me(api_client, world):
    sign_in(api_client, world.candidate)
    rows = ok(api_client.put("/v1/twin/scopes", json={"topics": [{"topic": "skills", "permission": "answer"}]}))[
        "topics"
    ]
    assert {t["topic"]: t["permission"] for t in rows}["skills"] == "answer"
    assert {t["topic"]: t["permission"] for t in rows}["experience"] == "ask_me"  # the fixture's row is gone
    assert ok(api_client.get("/v1/twin/scopes"))["topics"] == rows


@pytest.mark.parametrize("topic", ["current_pay", "contact_details"])
@pytest.mark.parametrize("permission", ["answer", "ask_me"])
def test_pay_and_contact_details_cannot_be_opened(api_client, engine, world, topic, permission):
    sign_in(api_client, world.candidate)
    refused(
        api_client.put("/v1/twin/scopes", json={"topics": [{"topic": topic, "permission": permission}]}),
        422,
        "topic_locked",
    )
    assert (
        w.one(engine, "SELECT count(*) FROM twin_scopes WHERE user_id = :u AND topic = 'experience'", u=world.candidate)
        == 1
    )
    ok(api_client.put("/v1/twin/scopes", json={"topics": [{"topic": topic, "permission": "never"}]}))


def test_unknown_topics_and_permissions_are_refused_and_save_nothing(api_client, engine, world):
    sign_in(api_client, world.candidate)
    for rows in (
        [{"topic": "hobbies", "permission": "answer"}],
        [{"topic": "skills", "permission": "maybe"}],
        [{"topic": "skills", "permission": "answer"}, {"topic": "skills", "permission": "never"}],
    ):
        refused(api_client.put("/v1/twin/scopes", json={"topics": rows}), 422, "validation_failed")
    assert w.one(engine, "SELECT permission::text FROM twin_scopes WHERE user_id = :u", u=world.candidate) == "answer"


def test_an_employer_neither_reads_nor_writes_the_candidates_scopes(api_client, engine, world):
    sign_in(api_client, world.member)
    mine = ok(api_client.get("/v1/twin/scopes"))["topics"]
    assert {t["topic"]: t["permission"] for t in mine}["experience"] == "ask_me"  # the member's own defaults
    ok(api_client.put("/v1/twin/scopes", json={"topics": [{"topic": "experience", "permission": "never"}]}))
    assert w.one(engine, "SELECT permission::text FROM twin_scopes WHERE user_id = :u", u=world.candidate) == "answer"


# ---- asking ----------------------------------------------------------------------------------------------------------


def test_an_employer_member_asks_and_the_answer_arrives_inside_the_wait(api_client, engine, world):
    sign_in(api_client, world.member)
    done = threading.Event()

    def worker() -> None:  # the worker process: runs jobs as they appear until the answer exists
        deadline = time.monotonic() + 20
        while not done.is_set() and time.monotonic() < deadline:
            w.run_jobs()
            time.sleep(0.1)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        data = ok(post(api_client, world), 201)
    finally:
        done.set()
        thread.join()
    assert data["state"] == "answered" and data["topic"] == "experience"
    assert data["answer"]["author"] == "twin" and data["answer"]["answer_kind"] == "answered"
    (citation,) = data["answer"]["citations"]
    assert citation["fact_id"] == str(world.world.candidate.record.fact_ids[1]) and "40 percent" in citation["quote"]
    assert "approval_id" not in data and "reply" not in data  # the employer's reading has no candidate keys


def test_without_a_worker_the_ask_comes_back_pending(api_client, world, no_wait):
    sign_in(api_client, world.member)
    data = ok(post(api_client, world), 201)
    assert data["state"] == "pending" and data["answer"] is None and data["topic"] is None
    assert data["question"]["body"] == "What did you build at your last job?"


def test_the_same_idempotency_key_asks_once(api_client, engine, world, no_wait):
    sign_in(api_client, world.member)
    headers = key()
    body = {"introduction_id": str(world.world.introduction), "question": "What did you build at your last job?"}
    first = ok(api_client.post("/v1/twin/questions", json=body, headers=headers), 201)
    again = api_client.post("/v1/twin/questions", json=body, headers=headers)
    assert ok(again, 201)["question_id"] == first["question_id"]
    assert again.json()["meta"]["idempotent_replay"] is True
    assert w.one(engine, "SELECT count(*) FROM twin_messages WHERE author = 'employer'") == 1


def test_an_introduction_the_caller_cannot_see_is_not_found(api_client, engine, world, no_wait):
    with engine.begin() as c:
        stranger = make_member(c, make_org(c, "Other Demo")).user
    sign_in(api_client, stranger)
    refused(post(api_client, world), 404, "not_found")
    refused(
        api_client.post(
            "/v1/twin/questions", json={"introduction_id": str(uuid4()), "question": "Hello there?"}, headers=key()
        ),
        404,
        "not_found",
    )


def test_a_candidate_cannot_ask(api_client, world, no_wait):
    sign_in(api_client, world.candidate)
    refused(post(api_client, world), 404, "not_found")


def test_a_closed_session_and_the_question_limit_are_conflicts(api_client, engine, world, no_wait):
    sign_in(api_client, world.member)
    ok(post(api_client, world), 201)
    with engine.begin() as c:
        for _ in range(twin.SESSION_QUESTIONS - 1):
            c.execute(
                text(
                    """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
                       VALUES (:s, :u, :o, 'employer', 'Another question?')"""
                ),
                {"s": world.session, "u": world.candidate, "o": world.world.org.id},
            )
    error = refused(post(api_client, world), 409, "conflict")
    assert error["details"]["state"] == "limit_reached"
    with engine.begin() as c:
        c.execute(
            text("UPDATE twin_sessions SET status = 'closed', closed_at = now() WHERE id = :s"), {"s": world.session}
        )
    assert refused(post(api_client, world), 409, "conflict")["details"]["state"] == "session_closed"


def test_a_withdrawn_introduction_consent_is_403_consent_required(api_client, engine, world, no_wait):
    sign_in(api_client, world.member)
    with engine.begin() as c:
        withdraw(c, world.world.accepted.consent)
    refused(post(api_client, world), 403, "consent_required")


def test_a_question_must_be_five_to_a_thousand_characters(api_client, world, no_wait):
    sign_in(api_client, world.member)
    for question in ("Hi?", "x" * 1001):
        refused(post(api_client, world, question), 422, "validation_failed")


# ---- reading ---------------------------------------------------------------------------------------------------------


def test_the_candidate_reads_every_session_with_every_question_answer_and_reply(api_client, engine, world, monkeypatch):
    w.use_fakes(monkeypatch, "unknown")
    first = w.ask(world, "What did you build at your last job?")
    second = w.ask(world, "When could you start?")
    w.run_jobs()
    waiting = w.pending(engine, second)
    w.approve(world, waiting.approval_id, waiting.payload, reply="I can start in 30 days.")
    sign_in(api_client, world.candidate)
    (session,) = ok(api_client.get("/v1/twin/questions"))["sessions"]
    assert session["session_id"] == str(world.session) and session["status"] == "open"
    assert session["organization"]["name"] == "Halden Demo" and session["role"]["title"]
    assert (session["questions_used"], session["questions_left"]) == (2, twin.SESSION_QUESTIONS - 2)
    one, two = session["questions"]
    assert [one["question_id"], two["question_id"]] == [str(first), str(second)]
    for view in (one, two):
        assert view["state"] == "waiting_for_candidate" and view["answer"]["body"] == logic.UNKNOWN_TEXT
        assert view["answer"]["answer_kind"] == "unknown"
    assert one["needs_reply"] is True and one["approval_id"] is not None and one["reply"] is None
    assert two["reply"]["body"] == "I can start in 30 days."  # their own reply, about them
    w.run_jobs()
    (session,) = ok(api_client.get("/v1/twin/questions"))["sessions"]
    assert (
        session["questions"][1]["state"] == "relayed"
        and session["questions"][1]["answer"]["body"] == "I can start in 30 days."
    )


def test_an_employer_reads_one_introductions_thread_and_never_the_reply(api_client, engine, world, monkeypatch):
    w.use_fakes(monkeypatch, "unknown")
    question = w.ask(world, "When could you start?")
    w.run_jobs()
    waiting = w.pending(engine, question)
    w.approve(world, waiting.approval_id, waiting.payload, reply="I can start in 30 days.")
    sign_in(api_client, world.member)
    data = ok(api_client.get("/v1/twin/questions", params={"introduction_id": str(world.world.introduction)}))
    (session,) = data["sessions"]
    assert session["introduction_id"] == str(world.world.introduction) and session["questions_used"] == 1
    (view,) = session["questions"]
    assert view["state"] == "waiting_for_candidate" and view["answer"]["body"] == logic.UNKNOWN_TEXT
    assert "reply" not in view and "approval_id" not in view and "needs_reply" not in view
    assert "I can start in 30 days" not in str(data)  # the candidate's words reach the employer only once relayed
    w.run_jobs()
    (view,) = ok(api_client.get("/v1/twin/questions", params={"introduction_id": str(world.world.introduction)}))[
        "sessions"
    ][0]["questions"]
    assert view["state"] == "relayed" and view["answer"]["body"] == "I can start in 30 days."


def test_an_employer_without_an_introduction_id_is_422_and_a_stranger_reads_nothing(api_client, engine, world):
    sign_in(api_client, world.member)
    refused(api_client.get("/v1/twin/questions"), 422, "validation_failed")
    with engine.begin() as c:
        stranger = make_member(c, make_org(c, "Other Demo")).user
    sign_in(api_client, stranger)
    data = ok(api_client.get("/v1/twin/questions", params={"introduction_id": str(world.world.introduction)}))
    assert data == {"sessions": []}


def test_a_person_with_no_conversations_reads_an_empty_list(api_client, engine):
    from tests.hiring.factories import make_candidate

    with engine.begin() as c:
        nobody = make_candidate(c).id
    sign_in(api_client, nobody)
    assert ok(api_client.get("/v1/twin/questions")) == {"sessions": []}


def test_the_routes_need_only_the_api_login(api_client, world, no_wait, monkeypatch):
    """Asking, reading and the scopes open no worker or match transaction (the other two logins are unreachable)."""
    from dataclasses import replace

    from app.platform import config

    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
    settings = replace(config.get(), database_url_worker=unreachable, database_url_match=unreachable)
    monkeypatch.setattr(config, "get", lambda: settings)
    db._engines.pop("worker", None)
    db._engines.pop("match", None)
    sign_in(api_client, world.member)
    ok(post(api_client, world), 201)
    ok(api_client.get("/v1/twin/questions", params={"introduction_id": str(world.world.introduction)}))
    sign_in(api_client, world.candidate)
    ok(api_client.get("/v1/twin/questions"))
    ok(api_client.get("/v1/twin/scopes"))
    assert set(db._engines) <= {"api"}
    assert UUID(str(world.session))


def test_no_module_a_route_reaches_opens_a_worker_or_match_transaction():
    """The API process holds only the API login: the modules the Twin's routes, the approve hook, the stream source
    and the audit source run through never name the other two roles (the graph, delivery and jobs are the worker's)."""
    from pathlib import Path

    import app.twin

    folder = Path(app.twin.__file__).parent
    for name in ("routes.py", "service.py", "subject.py", "stream.py", "audit_source.py", "api.py"):
        source = (folder / name).read_text()
        assert 'role="worker"' not in source and "role='worker'" not in source, name
        assert 'role="match"' not in source and "role='match'" not in source, name
