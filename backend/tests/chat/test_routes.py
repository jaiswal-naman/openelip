"""CHT-01, threats T-05-22, T-05-25 and T-05-28: the chat routes over HTTP (plan 05-04; API.md 2.8, 1.6, 1.8, 1.11).

The API runs on the test database as the real app_api role, with the real gateway on the fake provider. A person is
signed in by replacing the session dependency, as the other route tests do. Generation runs in the API process in a
background thread, so a test waits on the reply's own end (never a sleep) or holds the reply open inside a tool to look
at it while it runs. Everything here is invented.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.chat import hub
from app.trust import api as trust
from tests.chat.fixtures.service import world as chat_world
from tests.chat.fixtures.service.world import count, ok, open_session, post, refused, say, stored, transcript


@pytest.fixture
def w(api_client, engine, monkeypatch):
    world = chat_world.build(api_client, engine, monkeypatch)
    yield world
    chat_world.finish(engine)


@pytest.fixture
def w_off(api_client, engine, monkeypatch):
    """A real model with no key: nothing could answer. (The consent is given under that provider list.)"""
    world = chat_world.build(api_client, engine, monkeypatch, None, LLM_PROVIDER="anthropic")
    yield world
    chat_world.finish(engine)


def messages_of(w, person: UUID, session: str, n: int, *, minutes_ago: float) -> None:
    """n messages of this person's, the oldest `minutes_ago` minutes old and each of the rest a second newer."""
    with w.engine.begin() as c:
        for i in range(n):
            c.execute(
                text(
                    """INSERT INTO chat_messages (session_id, user_id, role, content, created_at)
                       VALUES (:s, :u, 'user', 'an earlier message', now() - :age)"""
                ),
                {"s": session, "u": person, "age": timedelta(minutes=minutes_ago) - i * timedelta(seconds=1)},
            )


# ---- sessions ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["open", "onboarding"])
def test_opening_a_session_returns_201_and_stores_it_with_the_kind_asked_for(w, kind):
    data = ok(post(w, "/v1/chat/sessions", {"kind": kind}), 201)["data"]

    assert set(data) == {"id", "kind", "created_at", "last_message_at"}
    assert (data["kind"], data["last_message_at"]) == (kind, None)
    with w.engine.connect() as conn:
        row = conn.execute(text("SELECT user_id, kind::text FROM chat_sessions WHERE id = :i"), {"i": data["id"]}).one()
    assert (row[0], row[1]) == (w.ada, kind)


def test_a_session_with_no_kind_is_an_open_one_and_an_unknown_kind_is_refused(w):
    assert ok(post(w, "/v1/chat/sessions", {}), 201)["data"]["kind"] == "open"
    error = refused(post(w, "/v1/chat/sessions", {"kind": "support"}), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "kind"


def test_listing_shows_only_the_callers_sessions_newest_activity_first(w):
    first, second = open_session(w), open_session(w)
    chat_world.sign_in(w.client, w.bob)
    bobs = open_session(w)
    chat_world.sign_in(w.client, w.ada)

    listed = ok(w.client.get("/v1/chat/sessions"))["data"]
    assert [s["id"] for s in listed] == [second, first]  # no activity yet: the newer session is first

    body = ok(say(w, first), 202)["data"]  # a message is activity
    chat_world.wait_done(body["message_id"])
    listed = ok(w.client.get("/v1/chat/sessions"))["data"]
    assert [s["id"] for s in listed] == [first, second] and bobs not in {s["id"] for s in listed}
    assert listed[0]["last_message_at"] is not None


# ---- posting a message ---------------------------------------------------------------------------------------------


def test_posting_returns_202_with_both_message_ids_and_stores_both_rows_before_the_reply_ends(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    gate = chat_world.hold(monkeypatch, "list_matches")
    session = open_session(w)

    data = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]

    assert data["status"] == "running"
    assert data["stream_url"] == f"/v1/chat/sessions/{session}/messages/{data['message_id']}/stream"
    assert gate.entered.wait(chat_world.WAIT_SECONDS)  # the reply is running: stored, not yet ended
    user, assistant = stored(w.engine, session)
    assert (str(user.id), user.role) == (data["user_message_id"], "user")
    assert user.content == "Why was the Globex job filtered?"
    assert (str(assistant.id), assistant.role, assistant.content) == (data["message_id"], "assistant", "")
    assert assistant.finish_reason is None
    assert ok(transcript(w, session))["data"][-1]["finish_reason"] is None  # still running
    gate.release.set()
    chat_world.wait_done(data["message_id"])


def test_the_same_key_gets_the_same_ids_and_starts_no_second_generation(w, monkeypatch):
    chat_world.record(monkeypatch, "two_messages.json")  # four answers: a second generation would use two more
    session = open_session(w)
    key = uuid4().hex

    first = ok(say(w, session, "What is shown?", key=key), 202)
    again = ok(say(w, session, "What is shown?", key=key), 202)
    chat_world.wait_done(first["data"]["message_id"])

    assert again["data"] == first["data"] and again["meta"]["idempotent_replay"] is True
    assert count(w.engine, "chat_messages WHERE role = 'user'") == 1
    assert count(w.engine, "chat_messages WHERE role = 'assistant'") == 1
    assert count(w.engine, "llm_calls") == 2  # one generation: the step and the reply


def test_a_write_needs_an_idempotency_key_and_a_json_body(w):
    url = f"/v1/chat/sessions/{open_session(w)}/messages"
    refused(w.client.post(url, json={"text": "hello"}), 400, "idempotency_key_missing")
    refused(
        w.client.post(url, content="text=hello", headers={"Idempotency-Key": uuid4().hex}),
        415,
        "unsupported_media_type",
    )
    assert count(w.engine, "chat_messages") == 0


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   \n\t "},
        {"text": "x" * 4001},
        {"text": "a\x00b"},
        {"text": 12},
        {},
        {"text": "hello", "context_job_id": 0},
        {"text": "hello", "context_job_id": -4},
        {"text": "hello", "context_job_id": 2**63},
        {"text": "hello", "context_job_id": "seven"},
        {"text": "hello", "user_id": str(uuid4())},
    ],
    ids=[
        "empty",
        "blank",
        "too-long",
        "nul",
        "number",
        "no-text",
        "job-zero",
        "job-negative",
        "job-big",
        "job-text",
        "a-person",
    ],
)
def test_a_message_that_cannot_be_one_is_refused_before_anything_is_stored(w, body):
    session = open_session(w)
    error = refused(post(w, f"/v1/chat/sessions/{session}/messages", body), 422, "validation_failed")
    assert error["details"]["fields"]
    assert count(w.engine, "chat_messages") == 0 and hub.running() == []


def test_text_that_cannot_be_stored_is_refused_too(w):
    """A lone surrogate is valid JSON but cannot be encoded: it would be a 500 at the insert, so it is a 422 here."""
    session = open_session(w)
    response = w.client.post(
        f"/v1/chat/sessions/{session}/messages",
        content='{"text": "bad \\ud800 surrogate"}',
        headers={"Idempotency-Key": uuid4().hex, "Content-Type": "application/json"},
    )
    refused(response, 422, "validation_failed")
    assert count(w.engine, "chat_messages") == 0


def test_the_longest_allowed_message_is_accepted(w):
    session = open_session(w)
    body = ok(say(w, session, "x" * 4000), 202)["data"]
    chat_world.wait_done(body["message_id"])


def test_a_context_job_that_is_not_in_the_index_is_not_found_and_nothing_is_stored(w):
    session = open_session(w)
    refused(say(w, session, "About this job", context_job_id=10_000_000), 404, "not_found")
    assert count(w.engine, "chat_messages") == 0


def test_without_the_ai_processing_consent_the_post_is_403_and_nothing_is_stored_or_started(w):
    session = open_session(w)
    with w.engine.begin() as c:
        trust.withdraw_consent(c, w.ada, "ai_processing")

    error = refused(say(w, session), 403, "consent_required")

    assert error["details"]["purpose"] == "ai_processing"
    assert count(w.engine, "chat_messages") == 0 and count(w.engine, "llm_calls") == 0 and hub.running() == []


def test_with_no_model_to_call_the_post_is_503_ai_disabled_and_nothing_is_stored(w_off):
    session = open_session(w_off)
    refused(say(w_off, session), 503, "ai_disabled")
    assert count(w_off.engine, "chat_messages") == 0 and count(w_off.engine, "llm_calls") == 0


def test_the_consent_is_asked_for_before_the_model_is(w_off):
    """With no consent and no model the person is told about the consent, which they can fix."""
    session = open_session(w_off)
    with w_off.engine.begin() as c:
        trust.withdraw_consent(c, w_off.ada, "ai_processing")
    refused(say(w_off, session), 403, "consent_required")


def test_the_31st_message_in_an_hour_is_429_with_the_wait_in_seconds(w):
    session = open_session(w)
    messages_of(w, w.ada, session, 30, minutes_ago=50)

    response = say(w, session)

    error = refused(response, 429, "rate_limited")
    wait = int(response.headers["Retry-After"])
    assert 590 <= wait <= 610  # the oldest of the 30 is 50 minutes old: ten more minutes
    assert error["details"]["retry_after_seconds"] == wait
    assert count(w.engine, "chat_messages WHERE role = 'user'") == 30 and hub.running() == []


def test_the_30th_message_is_still_accepted(w):
    session = open_session(w)
    messages_of(w, w.ada, session, 29, minutes_ago=50)
    body = ok(say(w, session), 202)["data"]
    chat_world.wait_done(body["message_id"])


def test_messages_older_than_an_hour_and_other_peoples_do_not_count(w):
    session = open_session(w)
    messages_of(w, w.ada, session, 30, minutes_ago=90)  # all of them more than an hour old
    chat_world.sign_in(w.client, w.bob)
    messages_of(w, w.bob, open_session(w), 30, minutes_ago=5)  # thirty of Bob's, just now
    chat_world.sign_in(w.client, w.ada)

    body = ok(say(w, session), 202)["data"]
    chat_world.wait_done(body["message_id"])


def test_while_a_reply_of_the_same_person_runs_another_post_is_409_reply_running(w, monkeypatch):
    chat_world.record(monkeypatch, "two_messages.json")
    gate = chat_world.hold(monkeypatch, "list_matches")
    session, other = open_session(w), open_session(w)
    first = ok(say(w, session, "What is shown?"), 202)["data"]
    assert gate.entered.wait(chat_world.WAIT_SECONDS)

    for target in (session, other):  # the rule is per person, not per session
        error = refused(say(w, target, "And another thing."), 409, "conflict")
        assert error["details"] == {"state": "reply_running"}
    assert count(w.engine, "chat_messages WHERE role = 'user'") == 1

    gate.release.set()
    chat_world.wait_done(first["message_id"])
    again = ok(say(w, session, "Tell me more about it."), 202)["data"]  # the first reply ended: the way is open
    chat_world.wait_done(again["message_id"])


def test_another_persons_reply_running_does_not_stop_this_person_posting(w, monkeypatch):
    chat_world.record(monkeypatch, "two_messages.json")
    gate = chat_world.hold(monkeypatch, "list_matches")
    first = ok(say(w, open_session(w), "What is shown?"), 202)["data"]
    assert gate.entered.wait(chat_world.WAIT_SECONDS)

    chat_world.sign_in(w.client, w.bob)
    second = ok(say(w, open_session(w), "Hello from Bob."), 202)["data"]
    chat_world.wait_done(second["message_id"])

    gate.release.set()
    chat_world.wait_done(first["message_id"])


def test_another_persons_session_is_404_for_every_route_and_so_is_one_that_does_not_exist(w):
    chat_world.sign_in(w.client, w.bob)
    bobs_session = open_session(w)
    chat_world.sign_in(w.client, w.ada)
    for session in (bobs_session, str(uuid4())):
        refused(say(w, session), 404, "not_found")
        refused(transcript(w, session), 404, "not_found")
        refused(post(w, f"/v1/chat/sessions/{session}/messages/{uuid4()}/stop", {}), 404, "not_found")
    assert count(w.engine, "chat_messages") == 0


# ---- the transcript ------------------------------------------------------------------------------------------------


def test_the_transcript_lists_user_tool_and_assistant_in_order_with_citations_and_how_the_reply_ended(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    session = open_session(w)
    body = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]
    chat_world.wait_done(body["message_id"])

    messages = ok(transcript(w, session))["data"]

    assert [m["role"] for m in messages] == ["user", "tool", "assistant"]
    user, tool, assistant = messages
    assert (user["id"], user["text"]) == (body["user_message_id"], "Why was the Globex job filtered?")
    assert (tool["tool_name"], tool["text"]) == ("list_matches", None)
    assert (assistant["id"], assistant["finish_reason"]) == (body["message_id"], "complete")
    assert assistant["text"].startswith("That job was filtered by your company exclusion [[d1]].")
    cited = [{k: v for k, v in c.items() if v is not None} for c in assistant["citations"]]
    assert cited == [
        {"alias": "d1", "kind": "decision", "decision_id": str(w.filtered)},
        {"alias": "c1", "kind": "constraint", "constraint_id": str(w.excluded)},
    ]
    assert assistant["proposals"] == [] and user["citations"] == [] and user["finish_reason"] is None
    times = [m["created_at"] for m in messages]
    assert times == sorted(times)


def test_an_empty_session_has_an_empty_transcript(w):
    assert ok(transcript(w, open_session(w)))["data"] == []


def test_a_reply_left_running_by_a_process_that_is_gone_is_returned_and_stored_as_interrupted(w):
    session = open_session(w)
    with w.engine.begin() as c:
        c.execute(
            text("INSERT INTO chat_messages (session_id, user_id, role, content) VALUES (:s, :u, 'user', 'hello')"),
            {"s": session, "u": w.ada},
        )
        orphan = c.execute(
            text(
                """INSERT INTO chat_messages (session_id, user_id, role, content, created_at)
                   VALUES (:s, :u, 'assistant', '', now() + interval '1 second') RETURNING id"""
            ),
            {"s": session, "u": w.ada},
        ).scalar_one()
    assert hub.get(orphan) is None  # this process knows nothing of it

    reply = ok(transcript(w, session))["data"][-1]

    assert (reply["id"], reply["finish_reason"]) == (str(orphan), "interrupted")
    assert stored(w.engine, session)[-1].finish_reason == "interrupted"


# ---- stop ----------------------------------------------------------------------------------------------------------


def test_stop_ends_a_running_reply_as_stopped_and_the_model_is_not_asked_again(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    gate = chat_world.hold(monkeypatch, "list_matches")
    session = open_session(w)
    body = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]
    assert gate.entered.wait(chat_world.WAIT_SECONDS)

    answer = ok(post(w, f"/v1/chat/sessions/{session}/messages/{body['message_id']}/stop", {}))["data"]
    assert answer == {"message_id": body["message_id"], "finish_reason": None}  # asked to stop; it has not ended yet
    gate.release.set()
    chat_world.wait_done(body["message_id"])

    assistant = stored(w.engine, session)[-1]
    assert (assistant.finish_reason, assistant.content) == ("stopped", "")
    assert count(w.engine, "llm_calls") == 1  # the step that called the tool; the reply was never asked for


def test_stopping_a_finished_reply_answers_200_and_changes_nothing(w):
    session = open_session(w)
    body = ok(say(w, session), 202)["data"]
    chat_world.wait_done(body["message_id"])

    answer = ok(post(w, f"/v1/chat/sessions/{session}/messages/{body['message_id']}/stop", {}))["data"]

    assert answer == {"message_id": body["message_id"], "finish_reason": "complete"}
    assert stored(w.engine, session)[-1].finish_reason == "complete"


def test_stop_is_404_for_another_persons_reply_a_user_message_and_an_unknown_id(w):
    session = open_session(w)
    body = ok(say(w, session), 202)["data"]
    chat_world.wait_done(body["message_id"])
    chat_world.sign_in(w.client, w.bob)
    refused(post(w, f"/v1/chat/sessions/{session}/messages/{body['message_id']}/stop", {}), 404, "not_found")
    chat_world.sign_in(w.client, w.ada)
    for message in (body["user_message_id"], str(uuid4())):  # a user's message has no reply to stop
        refused(post(w, f"/v1/chat/sessions/{session}/messages/{message}/stop", {}), 404, "not_found")
    other = open_session(w)  # the reply exists, but not in this session
    refused(post(w, f"/v1/chat/sessions/{other}/messages/{body['message_id']}/stop", {}), 404, "not_found")
