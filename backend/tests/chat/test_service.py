"""CHT-01, CHT-03, CHT-04, threats T-05-23, T-05-24, T-05-25, T-05-27: what a reply is, and what is kept (plan 05-04).

Generation runs in a background thread of the API process, whether or not a stream is open. These tests send a message
through the API and wait on the reply's own end (never a sleep), then read what was stored (the assistant row, the tool
rows, the session) and what was sent (the events in the reply hub, which are what a stream carries). The model is the
real gateway on the fake provider with the real cost ledger; the candidates and every answer are invented.
"""

import json
import logging
from uuid import UUID

import pytest
from sqlalchemy import text

from app.chat import loop, tools
from app.platform import db
from app.platform.llm import AIDisabled, ProviderUnavailable, fake, gateway
from app.profile import api as profile
from tests.chat.fixtures import harness
from tests.chat.fixtures.service import world as chat_world
from tests.chat.fixtures.service.world import count, ok, open_session, post, say, stored

REPLY_TO_FILTERED = (
    "That job was filtered by your company exclusion [[d1]]. The constraint [[c1]] rules that employer out."
)
OUTSIDE_CHAT = (
    "outbox",
    "queue_jobs",
    "profile_events",
    "profile_facts",
    "constraints",
    "preferences",
    "consents",
    "documents",
    "match_runs",
    "match_decisions",
    "briefs",
    "tailored_resumes",
    "resume_edits",
    "application_packets",
    "applications",
    "application_events",
)


@pytest.fixture
def w(api_client, engine, monkeypatch):
    world = chat_world.build(api_client, engine, monkeypatch)
    yield world
    chat_world.finish(engine)


def ask(w, words: str, *, session: str | None = None, **extra):
    """Send a message, wait for the reply to end, and return (session, accepted body, the reply in the hub)."""
    session = session or open_session(w)
    body = ok(say(w, session, words, **extra), 202)["data"]
    return session, body, chat_world.wait_done(body["message_id"])


def names(reply) -> list[str]:
    return [event.name for event in reply.since(0)]


def error_of(reply):
    return next(event.data for event in reply.since(0) if event.name == "error")


def record_state(w) -> tuple:
    """What a proposal must leave as it was: the record's version and its preferences."""
    with db.transaction(w.ada) as conn:
        record = profile.get_record(conn, w.ada)
    return record["record_version"], [(p["kind"], p["value"]) for p in record["preferences"]]


# ---- what a finished reply stores ----------------------------------------------------------------------------------


def test_a_finished_reply_is_stored_with_its_text_citations_and_how_it_ended(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    session, body, reply = ask(w, "Why was the Globex job filtered?")

    user, tool, assistant = stored(w.engine, session)

    assert (user.role, tool.role, assistant.role) == ("user", "tool", "assistant")
    assert (assistant.content, assistant.finish_reason) == (REPLY_TO_FILTERED, "complete")
    refs = assistant.refs
    assert refs["citations"] == [
        {"alias": "d1", "kind": "decision", "decision_id": str(w.filtered)},
        {"alias": "c1", "kind": "constraint", "constraint_id": str(w.excluded)},
    ]
    assert (refs["proposals"], refs["proposal_event_ids"], refs["unknown_citations"]) == ([], [], 0)
    end = reply.since(0)[-1]
    assert end.name == "message.end" and refs["last_event_id"] == end.id
    assert refs["usage"] == {"input_tokens": 200, "output_tokens": 40} and refs["cost_usd"] == "0.000000"
    assert end.data["usage"] == refs["usage"] and end.data["cost_usd"] == refs["cost_usd"]

    assert (tool.tool_name, tool.content, tool.finish_reason) == ("list_matches", None, None)
    assert tool.refs == {
        "reply_id": body["message_id"],
        "aliases": {
            "d1": {"kind": "decision", "id": str(w.filtered)},
            "j1": {"kind": "job", "id": str(w.blocked.id)},
            "c1": {"kind": "constraint", "id": str(w.excluded)},
        },
    }
    with w.engine.connect() as conn:
        made, moved = conn.execute(
            text("SELECT created_at, last_message_at FROM chat_sessions WHERE id = :s"), {"s": session}
        ).one()
    assert moved is not None and moved >= assistant.created_at > tool.created_at > user.created_at >= made


def test_a_citation_no_tool_returned_is_plain_text_in_the_stored_reply_and_counted(w, monkeypatch):
    chat_world.record(monkeypatch, "cites_unknown.json")
    session, _, reply = ask(w, "Tell me about the jobs.")

    assistant = stored(w.engine, session)[-1]

    assert assistant.content == "See d9 and j4 for the details."
    assert (assistant.refs["citations"], assistant.refs["unknown_citations"]) == ([], 2)
    assert "citation" not in names(reply)


def test_the_open_job_is_the_alias_j1_and_the_session_remembers_it_for_the_next_message(w, monkeypatch):
    chat_world.record(monkeypatch, "open_job.json")
    session, _, _ = ask(w, "What do you think of this job?", context_job_id=w.ok.id)
    ask(w, "And is it still this one?", session=session)  # no open job this time

    _, first, _, second = stored(w.engine, session)
    for assistant in (first, second):
        assert assistant.refs["citations"] == [{"alias": "j1", "kind": "job", "job_id": w.ok.id}]
    assert first.refs["aliases"] == {"j1": {"kind": "job", "id": str(w.ok.id)}}  # kept with the first reply


def test_earlier_messages_and_the_sessions_aliases_reach_the_model_with_the_next_message(w, monkeypatch):
    chat_world.record(monkeypatch, "two_messages.json")
    sent, real = [], gateway._call

    def spy(route, messages, schema):
        sent.append([dict(m) for m in messages])
        return real(route, messages, schema)

    monkeypatch.setattr(gateway, "_call", spy)
    session, _, _ = ask(w, "What is shown?")
    ask(w, "Show me the evidence for it.", session=session)

    opening = sent[2]  # the first model call of the second message
    assert [m["role"] for m in opening] == ["system", "user", "assistant", "user"]
    assert opening[1]["content"] == "What is shown?"
    assert opening[2]["content"] == "You have one shown match [[d1]]."
    assert opening[3]["content"].endswith("Show me the evidence for it.")
    # d1 of the first message is still that decision: the second message's tool found it and its reply cites it
    _, _, _, _, tool, assistant = stored(w.engine, session)
    assert tool.tool_name == "get_decision" and tool.refs["aliases"]["d1"] == {"kind": "decision", "id": str(w.shown)}
    assert assistant.refs["citations"] == [{"alias": "d1", "kind": "decision", "decision_id": str(w.shown)}]


def test_a_new_post_marks_a_reply_that_a_dead_process_left_running_as_interrupted(w):
    session = open_session(w)
    with w.engine.begin() as c:
        orphan = c.execute(
            text(
                """INSERT INTO chat_messages (session_id, user_id, role, content) VALUES (:s, :u, 'assistant', '')
                   RETURNING id"""
            ),
            {"s": session, "u": w.ada},
        ).scalar_one()

    ask(w, "Hello again.", session=session)

    by_id = {row.id: row for row in stored(w.engine, session)}
    assert by_id[orphan].finish_reason == "interrupted"


# ---- how a reply can end -------------------------------------------------------------------------------------------


def test_a_model_that_stops_answering_ends_the_reply_interrupted_keeps_what_was_written_and_sends_an_error(
    w, monkeypatch
):
    chat_world.record(monkeypatch, "ask_filtered.json")
    real, calls = gateway._call, []

    def flaky(route, messages, schema):
        calls.append(route.task)
        if len(calls) == 2:
            raise ProviderUnavailable()
        return real(route, messages, schema)

    monkeypatch.setattr(gateway, "_call", flaky)
    session, _, reply = ask(w, "Why was the Globex job filtered?")

    _, tool, assistant = stored(w.engine, session)
    assert (assistant.finish_reason, assistant.content) == ("interrupted", "")
    assert tool.tool_name == "list_matches"  # what was written is kept
    assert error_of(reply) == {
        "code": "dependency_unavailable",
        "message": "The AI model provider is not answering. Try again shortly.",
        "is_retryable": True,
    }
    assert names(reply)[-2:] == ["error", "message.end"] and reply.since(0)[-1].data["finish_reason"] == "interrupted"
    with w.engine.connect() as conn:
        outcomes = list(conn.execute(text("SELECT outcome::text FROM llm_calls ORDER BY id")).scalars())
    assert outcomes == ["ok", "provider_error"]


def test_with_nothing_to_answer_the_task_the_reply_ends_interrupted_with_ai_disabled_and_no_retry(w, monkeypatch):
    def no_recording(task):
        raise AIDisabled("no_recording")

    monkeypatch.setattr(fake, "call", no_recording)
    session, _, reply = ask(w, "Hello?")

    assert stored(w.engine, session)[-1].finish_reason == "interrupted"
    assert error_of(reply) == {
        "code": "ai_disabled",
        "message": "This feature needs an AI model and none is configured.",
        "is_retryable": False,
    }


def test_a_model_that_answers_with_nonsense_twice_is_reported_as_unavailable_and_may_be_retried(
    w, monkeypatch, tmp_path
):
    monkeypatch.setattr(fake, "_source", harness.write_answers(tmp_path, "this is not json", "nor is this"))
    monkeypatch.setattr(fake, "_given", {})
    session, _, reply = ask(w, "Hello?")

    assert stored(w.engine, session)[-1].finish_reason == "interrupted"
    assert error_of(reply)["code"] == "dependency_unavailable" and error_of(reply)["is_retryable"] is True
    assert "not json" not in json.dumps(error_of(reply))  # the answer itself is never repeated


def test_a_tool_that_fails_ends_the_reply_interrupted_and_the_error_says_nothing_of_the_failure(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")

    def broken(user_id, args, aliases):
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(tools, "list_matches", broken)
    session, _, reply = ask(w, "Why was the Globex job filtered?")

    assert names(reply) == ["message.start", "tool.start", "error", "message.end"]
    assert error_of(reply) == {
        "code": "internal_error",
        "message": "Something went wrong on our side.",
        "is_retryable": False,
    }
    assert "on fire" not in json.dumps([event.data for event in reply.since(0)])
    rows = stored(w.engine, session)
    assert [r.role for r in rows] == ["user", "assistant"] and rows[-1].finish_reason == "interrupted"


def test_a_model_that_fails_inside_a_tool_is_reported_as_the_provider_and_not_as_our_bug(w, monkeypatch):
    chat_world.record(monkeypatch, "propose_pune.json")
    real, calls = gateway._call, []

    def flaky(route, messages, schema):
        calls.append(route.task)
        if route.task == "record.propose":
            raise ProviderUnavailable()
        return real(route, messages, schema)

    monkeypatch.setattr(gateway, "_call", flaky)
    session, _, reply = ask(w, "I would also take jobs in Pune.")

    assert stored(w.engine, session)[-1].finish_reason == "interrupted"
    assert error_of(reply)["code"] == "dependency_unavailable" and error_of(reply)["is_retryable"] is True


def test_a_reply_over_the_token_limit_ends_as_length_with_nothing_written(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    monkeypatch.setattr(loop, "MAX_TOKENS", 100)  # the first model call alone spends 120: the ledger sum is what counts
    session, _, reply = ask(w, "Why was the Globex job filtered?")

    assistant = stored(w.engine, session)[-1]
    assert (assistant.finish_reason, assistant.content) == ("length", "")
    assert count(w.engine, "llm_calls") == 1 and "error" not in names(reply)
    assert reply.since(0)[-1].data["finish_reason"] == "length"


# ---- CHT-03 and CHT-04 ---------------------------------------------------------------------------------------------


def test_a_proposal_from_chat_waits_for_the_candidate_and_confirming_it_applies_it(w, monkeypatch):
    chat_world.record(monkeypatch, "propose_pune.json")
    before = record_state(w)

    session, _, reply = ask(w, "I would also take jobs in Pune.")

    proposal = next(event.data for event in reply.since(0) if event.name == "proposal")
    assert (proposal["entity"], proposal["operation"], proposal["statement"]) == (
        "preference",
        "create",
        "Open to jobs in Pune.",
    )
    assistant = stored(w.engine, session)[-1]
    assert assistant.refs["proposal_event_ids"] == [proposal["event_id"]] and assistant.refs["proposals"] == [proposal]
    with w.engine.connect() as conn:
        event = conn.execute(
            text("SELECT event_type::text, origin::text FROM profile_events WHERE id = :i"),
            {"i": proposal["event_id"]},
        ).one()
    assert tuple(event) == ("proposed", "chat")
    assert record_state(w) == before  # a proposal is not a change: not the version, not the preferences

    confirmed = ok(post(w, f"/v1/me/events/{proposal['event_id']}/confirm", {}))["data"]  # the candidate's own click

    assert confirmed["entity"] == "preference"
    version, preferences = record_state(w)
    assert version == before[0] + 1 and ("location", "Pune") in preferences


def test_yes_send_it_changes_nothing_outside_the_chat_rows(w, monkeypatch):
    chat_world.record(monkeypatch, "yes_send_it.json")
    before = {table: count(w.engine, table) for table in OUTSIDE_CHAT}
    version = record_state(w)

    session, _, reply = ask(w, "Yes, send it.")

    assert {table: count(w.engine, table) for table in OUTSIDE_CHAT} == before
    assert record_state(w) == version
    assert [row.role for row in stored(w.engine, session)] == ["user", "tool", "assistant"]
    assert "proposal" not in names(reply)


def test_nothing_of_another_person_reaches_a_reply_its_rows_or_its_events(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    session, _, reply = ask(w, "Why was the Globex job filtered?")

    everything = json.dumps(
        [
            [(event.name, event.data) for event in reply.since(0)],
            [(row.content, row.refs) for row in stored(w.engine, session)],
        ],
        default=str,
    )

    for other in (w.bob, w.bobs_shown, w.bobs_withheld, "Bob's own line."):
        assert str(other) not in everything


# ---- cost and the log ----------------------------------------------------------------------------------------------


def a_registered_model(engine) -> UUID:
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
                   VALUES ('fake', 'recorded', 'chat', true, 'invented')
                   ON CONFLICT (provider, model_name) DO NOTHING"""
            )
        )
        return c.execute(text("SELECT id FROM model_registry ORDER BY id LIMIT 1")).scalar_one()


def ledger_row(engine, user: UUID, model: UUID, task: str, tokens_in: int, tokens_out: int, micro: int) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, input_tokens, output_tokens,
                                          cost_microusd, latency_ms)
                   VALUES (:task, 'invented', :model, 'ok', :u, :i, :o, :c, 10)"""
            ),
            {"task": task, "model": model, "u": user, "i": tokens_in, "o": tokens_out, "c": micro},
        )


def test_message_end_sums_this_replys_chat_and_proposal_calls_and_leaves_out_everything_else(w, monkeypatch):
    chat_world.record(monkeypatch, "propose_pune.json")
    monkeypatch.setattr(gateway, "cost_microusd", lambda *args: 5000)  # a priced route; the fake provider has no price
    model = a_registered_model(w.engine)
    ledger_row(w.engine, w.ada, model, "chat.turn", 9000, 9000, 777)  # an earlier message of hers: not this reply's
    real_build = tools.build_tools

    def with_tailoring_in_the_background(user_text):
        built = real_build(user_text)
        inner = built["propose_record_change"]

        def propose(user_id, args, aliases):
            ledger_row(w.engine, user_id, model, "tailor.propose", 5000, 7000, 9999)  # the worker's, mid-reply
            return inner(user_id, args, aliases)

        built["propose_record_change"] = propose
        return built

    monkeypatch.setattr(tools, "build_tools", with_tailoring_in_the_background)
    _, _, reply = ask(w, "I would also take jobs in Pune.")

    end = reply.since(0)[-1]
    # two chat.turn calls and one record.propose call, 100 in and 20 out each, 5,000 micro-dollars each
    assert end.data["usage"] == {"input_tokens": 300, "output_tokens": 60}
    assert end.data["cost_usd"] == "0.015000"


def test_the_log_carries_ids_counts_and_codes_never_the_conversation(w, monkeypatch, caplog):
    chat_world.record(monkeypatch, "ask_filtered.json")
    with caplog.at_level(logging.INFO, logger="app.event"):
        ask(w, "Why was the Globex job filtered?")

    lines = [json.loads(record.getMessage()) for record in caplog.records if record.name == "app.event"]
    chat = [line for line in lines if line["event"].startswith("chat.")]
    assert {"chat.turn", "chat.reply"} <= {line["event"] for line in chat}
    reply_line = next(line for line in chat if line["event"] == "chat.reply")
    assert reply_line["outcome"] == "complete" and reply_line["input_tokens"] == 200
    assert not any(word in json.dumps(lines) for word in ("Globex", "exclusion", "Why was"))  # nothing said or answered
