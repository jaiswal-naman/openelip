"""CHT-01 (SC1) and threat T-05-26: the reply event stream (plan 05-04; API.md 4.1 and 4.2).

A stream is a GET that serves the events of one reply: `retry: 3000`, then message.start (id 1), the reply's events
with rising ids, message.end, then the stream closes. A reconnect with Last-Event-ID is sent a snapshot first. A reply
that is no longer in the hub is built from the database. The API runs on the test database with the real gateway on the
fake provider; a test either runs a real reply and reads its stream, or puts a reply into the hub by hand so that it can
be in exactly the state the test needs. Everything here is invented.
"""

import asyncio
import inspect
import json
import threading
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.chat import hub, service, stream
from app.platform.llm import ProviderUnavailable, gateway
from tests.chat.fixtures.service import world as chat_world
from tests.chat.fixtures.service.world import frames, ok, open_session, refused, say, stored, transcript

REPLY = "That job was filtered by your company exclusion [[d1]]. The constraint [[c1]] rules that employer out."


@pytest.fixture
def w(api_client, engine, monkeypatch):
    world = chat_world.build(api_client, engine, monkeypatch)
    yield world
    chat_world.finish(engine)


def read(w, url: str, **headers):
    """Open the stream, read it to its end, and return (status, headers, the frames, the raw body)."""
    with w.client.stream("GET", url, headers=headers) as response:
        body = "".join(response.iter_text())
        return response.status_code, response.headers, frames(body), body


def live(response):
    """The frames of an open stream, one at a time as they arrive."""
    block: list[str] = []
    for line in response.iter_lines():
        if line:
            block.append(line)
        elif block:
            yield frames("\n".join(block) + "\n\n")[0]
            block = []


def a_reply_row(w, *, finish_reason=None, content="", refs=None) -> tuple[str, UUID]:
    """A session of Ada's with one assistant row, the way the database holds a reply (no hub entry)."""
    session = open_session(w)
    with w.engine.begin() as c:
        message = c.execute(
            text(
                """INSERT INTO chat_messages (session_id, user_id, role, content, finish_reason, refs)
                   VALUES (:s, :u, 'assistant', :c, :f, CAST(:r AS jsonb)) RETURNING id"""
            ),
            {"s": session, "u": w.ada, "c": content, "f": finish_reason, "r": json.dumps(refs or {})},
        ).scalar_one()
    return session, message


def a_reply_in_the_hub(w) -> tuple[str, UUID, hub.Reply]:
    session, message = a_reply_row(w)
    return session, message, hub.start(message, UUID(session), w.ada)


def url_of(session: str, message: UUID | str) -> str:
    return f"/v1/chat/sessions/{session}/messages/{message}/stream"


def end_of(reply: hub.Reply, reason: str = "complete") -> None:
    reply.emit(
        "message.end",
        {
            "message_id": str(reply.message_id),
            "finish_reason": reason,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "cost_usd": "0.000000",
        },
    )
    reply.finish()


# ---- the format ----------------------------------------------------------------------------------------------------


def test_a_frame_is_one_data_line_of_json_whatever_the_text_holds():
    data = {"text": 'line1\nline2\r\nline3 \u0085 café "quoted" \x00 \\'}
    raw = stream.frame(4, "text.delta", data)

    assert raw == "id: 4\nevent: text.delta\ndata: " + json.dumps(data, separators=(",", ":")) + "\n\n"
    assert raw.count("\n") == 4 and len(raw.splitlines()) == 4  # no line break inside the data, in any line splitting
    assert frames(raw) == [{"id": 4, "event": "text.delta", "data": data}]
    assert stream.frame(None, "message.snapshot", {}) == "event: message.snapshot\ndata: {}\n\n"  # no id line


def test_last_event_id_is_a_number_or_it_is_ignored():
    assert [stream.parse_last_event_id(v) for v in (None, "", "3", " 12 ", "0", "abc", "-1", "1.5", "9" * 40)] == [
        None,
        None,
        3,
        12,
        0,
        None,
        None,
        None,
        None,
    ]


def test_the_stream_response_is_text_event_stream_that_nothing_may_cache_or_buffer(w):
    session, message, reply = a_reply_in_the_hub(w)
    end_of(reply)

    status, headers, got, _ = read(w, url_of(session, message))

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream") and "utf-8" in headers["content-type"].lower()
    assert (headers["cache-control"], headers["x-accel-buffering"]) == ("no-store", "no")
    assert got[0] == {"retry": 3000}


def test_the_openapi_document_describes_the_stream_as_text_event_stream(w):
    paths = w.client.get("/openapi.json").json()["paths"]
    operation = paths["/v1/chat/sessions/{session_id}/messages/{message_id}/stream"]["get"]
    assert "text/event-stream" in operation["responses"]["200"]["content"] and "204" in operation["responses"]
    assert set(paths["/v1/chat/sessions"]) == {"get", "post"}
    assert set(paths["/v1/chat/sessions/{session_id}/messages"]) == {"get", "post"}
    assert set(paths["/v1/chat/sessions/{session_id}/messages/{message_id}/stop"]) == {"post"}


# ---- a reply streamed live: CHT-01 ----------------------------------------------------------------------------------


def test_asking_about_a_decision_streams_tool_result_with_its_id_and_a_citation_carrying_that_decisions_ids(
    w, monkeypatch
):
    chat_world.record(monkeypatch, "ask_filtered.json")
    gate = chat_world.hold(monkeypatch, "list_matches")
    session = open_session(w)
    body = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]
    assert gate.entered.wait(chat_world.WAIT_SECONDS)  # the reply is running, with its tool in hand

    seen = []
    with w.client.stream("GET", body["stream_url"]) as response:
        for frame in live(response):
            seen.append(frame)
            if frame.get("event") == "tool.start":
                gate.release.set()  # the tool answers now, and the rest of the reply follows

    assert seen[0] == {"retry": 3000}
    events = seen[1:]
    assert [f["id"] for f in events] == list(range(1, len(events) + 1))
    assert [f["event"] for f in events][:3] == ["message.start", "tool.start", "tool.result"]
    assert events[0]["data"] == {"message_id": body["message_id"], "session_id": session}
    assert events[1]["data"] == {"call_id": "call_1", "tool": "list_matches"}

    result = events[2]["data"]  # only ids: no aliases, no text of what the tool read
    assert set(result) == {"call_id", "tool", "refs"} and result["tool"] == "list_matches"
    assert result["refs"]["decision_ids"] == [str(w.filtered)]
    assert result["refs"]["constraint_ids"] == [str(w.excluded)]

    assert "".join(f["data"]["text"] for f in events if f["event"] == "text.delta") == REPLY
    citations = [f["data"] for f in events if f["event"] == "citation"]
    assert citations == [
        {"alias": "d1", "kind": "decision", "decision_id": str(w.filtered)},
        {"alias": "c1", "kind": "constraint", "constraint_id": str(w.excluded)},
    ]
    end = events[-1]
    assert (end["event"], end["data"]["finish_reason"], end["data"]["message_id"]) == (
        "message.end",
        "complete",
        body["message_id"],
    )
    assert end["data"]["usage"] == {"input_tokens": 200, "output_tokens": 40} and end["data"]["cost_usd"] == "0.000000"

    # the stored transcript says the same thing the stream did
    assistant = ok(transcript(w, session))["data"][-1]
    assert assistant["text"] == REPLY
    assert [{k: v for k, v in c.items() if v is not None} for c in assistant["citations"]] == citations


def test_a_stream_of_a_reply_that_failed_ends_with_the_error_and_then_message_end(w, monkeypatch):
    chat_world.record(monkeypatch, "ask_filtered.json")
    real, calls = gateway._call, []

    def flaky(route, messages, schema):
        calls.append(route.task)
        if len(calls) == 2:
            raise ProviderUnavailable()
        return real(route, messages, schema)

    monkeypatch.setattr(gateway, "_call", flaky)
    session = open_session(w)
    body = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]
    chat_world.wait_done(body["message_id"])

    _, _, got, _ = read(w, body["stream_url"])

    error, end = got[-2], got[-1]
    assert (error["event"], error["data"]["code"], error["data"]["is_retryable"]) == (
        "error",
        "dependency_unavailable",
        True,
    )
    assert (end["event"], end["data"]["finish_reason"]) == ("message.end", "interrupted")


# ---- reconnecting --------------------------------------------------------------------------------------------------


def test_a_reconnect_gets_a_snapshot_of_everything_so_far_and_then_only_what_comes_after(w):
    session, message, reply = a_reply_in_the_hub(w)
    reply.emit("tool.start", {"call_id": "call_1", "tool": "list_matches"})  # 2
    reply.emit("tool.result", {"call_id": "call_1", "tool": "list_matches", "refs": {}})  # 3
    reply.emit("text.delta", {"text": "That job was filtered. "})  # 4
    reply.emit("text.delta", {"text": "Your floor is 30 lakh."})  # 5
    cited = {"alias": "d1", "kind": "decision", "decision_id": str(w.filtered)}
    reply.emit("citation", cited)  # 6
    proposed = {"event_id": str(uuid4()), "entity": "preference", "operation": "create", "statement": "Pune."}
    reply.emit("proposal", proposed)  # 7

    got = []
    with w.client.stream("GET", url_of(session, message), headers={"Last-Event-ID": "3"}) as response:
        for frame in live(response):
            got.append(frame)
            if frame.get("event") == "message.snapshot":  # now the reply goes on
                reply.emit("text.delta", {"text": " More."})  # 8
                end_of(reply)  # 9

    assert got[0] == {"retry": 3000}
    assert got[1] == {
        "id": 7,
        "event": "message.snapshot",
        "data": {
            "text": "That job was filtered. Your floor is 30 lakh.",
            "citations": [cited],
            "proposals": [proposed],
        },
    }
    assert [(f["id"], f["event"]) for f in got[2:]] == [(8, "text.delta"), (9, "message.end")]  # nothing sent twice


def test_a_first_connection_to_a_reply_already_under_way_replays_it_from_the_first_event(w):
    session, message, reply = a_reply_in_the_hub(w)
    reply.emit("text.delta", {"text": "Already written. "})
    reply.emit("text.delta", {"text": "And more."})
    threading.Timer(0.2, end_of, args=(reply,)).start()

    _, _, got, _ = read(w, url_of(session, message))

    assert [(f["id"], f["event"]) for f in got[1:]] == [
        (1, "message.start"),
        (2, "text.delta"),
        (3, "text.delta"),
        (4, "message.end"),
    ]


def test_a_reply_that_ended_while_the_client_was_away_is_sent_as_a_snapshot_and_message_end(w):
    session, message, reply = a_reply_in_the_hub(w)
    reply.emit("text.delta", {"text": "All of it."})
    end_of(reply)

    _, _, got, _ = read(w, url_of(session, message), **{"Last-Event-ID": "2"})

    assert [f.get("event") for f in got[1:]] == ["message.snapshot", "message.end"]
    assert got[1]["data"]["text"] == "All of it." and got[1]["id"] == 2
    assert got[2]["id"] == 3 and got[2]["data"]["finish_reason"] == "complete"


def test_a_finished_reply_is_204_for_a_client_that_has_seen_its_message_end(w):
    session, message, reply = a_reply_in_the_hub(w)
    end_of(reply)  # ids 1 (start) and 2 (end)

    response = w.client.get(url_of(session, message), headers={"Last-Event-ID": "2"})

    assert response.status_code == 204 and response.content == b""


def test_a_last_event_id_that_is_not_a_number_is_ignored_like_a_first_connection(w):
    session, message, reply = a_reply_in_the_hub(w)
    end_of(reply)

    _, _, got, _ = read(w, url_of(session, message), **{"Last-Event-ID": "not-a-number"})

    assert [(f["id"], f["event"]) for f in got[1:]] == [(1, "message.start"), (2, "message.end")]


# ---- a reply the hub has forgotten ---------------------------------------------------------------------------------


def test_a_finished_reply_the_hub_no_longer_has_is_served_from_the_database_and_204_ends_the_reconnecting(
    w, monkeypatch
):
    chat_world.record(monkeypatch, "ask_filtered.json")
    session = open_session(w)
    body = ok(say(w, session, "Why was the Globex job filtered?"), 202)["data"]
    reply = chat_world.wait_done(body["message_id"])
    last_id = reply.last_id
    monkeypatch.setattr(hub, "HUB_KEEP_SECONDS", 0)  # the next look forgets it

    status, _, got, _ = read(w, body["stream_url"])

    assert hub.get(reply.message_id) is None and status == 200
    assert [f.get("event") for f in got[1:]] == ["message.snapshot", "message.end"]
    snapshot, end = got[1], got[2]
    assert "id" not in snapshot and snapshot["data"]["text"] == REPLY and snapshot["data"]["proposals"] == []
    assert snapshot["data"]["citations"] == [
        {"alias": "d1", "kind": "decision", "decision_id": str(w.filtered)},
        {"alias": "c1", "kind": "constraint", "constraint_id": str(w.excluded)},
    ]
    assert end["id"] == last_id  # the number the live stream gave message.end
    assert end["data"] == {
        "message_id": body["message_id"],
        "finish_reason": "complete",
        "usage": {"input_tokens": 200, "output_tokens": 40},
        "cost_usd": "0.000000",
    }

    assert w.client.get(body["stream_url"], headers={"Last-Event-ID": str(last_id)}).status_code == 204
    _, _, again, _ = read(w, body["stream_url"], **{"Last-Event-ID": "1"})  # saw less than the end: sent it again
    assert [f.get("event") for f in again[1:]] == ["message.snapshot", "message.end"]


def test_a_reply_a_dead_process_left_running_streams_as_interrupted_and_is_stored_as_that(w):
    session, message = a_reply_row(w)  # finish_reason NULL, and this process is not writing it

    status, _, got, _ = read(w, url_of(session, message))

    assert status == 200 and [f.get("event") for f in got[1:]] == ["message.snapshot", "message.end"]
    end = got[2]
    assert end["data"] == {
        "message_id": str(message),
        "finish_reason": "interrupted",
        "usage": None,
        "cost_usd": None,
    }
    assert end["id"] == service.ORPHAN_END_ID and got[1]["data"] == {"text": "", "citations": [], "proposals": []}
    assert stored(w.engine, session)[-1].finish_reason == "interrupted"
    response = w.client.get(url_of(session, message), headers={"Last-Event-ID": str(service.ORPHAN_END_ID)})
    assert response.status_code == 204


# ---- keep-alive ----------------------------------------------------------------------------------------------------


def test_while_no_event_arrives_a_ping_comment_is_sent_every_ping_seconds(w, monkeypatch):
    monkeypatch.setattr(stream, "PING_SECONDS", 0.05)
    monkeypatch.setattr(stream, "POLL_SECONDS", 0.01)
    session, message, reply = a_reply_in_the_hub(w)
    ender = threading.Timer(0.5, end_of, args=(reply,))
    ender.start()

    _, _, got, body = read(w, url_of(session, message))
    ender.join()

    pings = [f for f in got if f.get("comment") == "ping"]
    assert len(pings) >= 3 and ": ping\n\n" in body
    assert got[0] == {"retry": 3000} and got[1]["event"] == "message.start" and got[-1]["event"] == "message.end"


# ---- who may read a stream -----------------------------------------------------------------------------------------


def test_another_persons_stream_and_anything_that_is_not_a_reply_of_the_session_is_404(w, monkeypatch):
    chat_world.record(monkeypatch, "plain_reply.json")
    session = open_session(w)
    body = ok(say(w, session, "Hello"), 202)["data"]
    chat_world.wait_done(body["message_id"])
    other = open_session(w)

    chat_world.sign_in(w.client, w.bob)
    refused(w.client.get(body["stream_url"]), 404, "not_found")
    chat_world.sign_in(w.client, w.ada)
    for url in (
        url_of(session, uuid4()),  # no such reply
        url_of(session, body["user_message_id"]),  # a candidate's message is not a reply
        url_of(other, body["message_id"]),  # a reply of another session
        url_of(uuid4(), body["message_id"]),  # no such session
    ):
        refused(w.client.get(url), 404, "not_found")


# ---- no thread is held while a stream waits ------------------------------------------------------------------------


def test_the_stream_generators_are_async_so_waiting_costs_no_worker_thread():
    assert inspect.isasyncgenfunction(stream.from_hub) and inspect.isasyncgenfunction(stream.from_row)


def test_two_hundred_streams_on_one_running_reply_use_no_thread_and_never_block_the_event_loop():
    reply = hub.Reply(uuid4(), uuid4(), uuid4())
    reply.emit("message.start", {})
    before = threading.active_count()
    ticks = 0

    async def watcher():
        nonlocal ticks
        while not reply.finished.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    async def consume():
        return [chunk async for chunk in stream.from_hub(reply, None)]

    async def main():
        streams = [asyncio.create_task(consume()) for _ in range(200)]
        watching = asyncio.create_task(watcher())
        await asyncio.sleep(0.3)  # 200 streams are waiting on a reply that says nothing
        threads_while_waiting = threading.active_count()
        end_of(reply)
        results = await asyncio.gather(*streams)
        await watching
        return results, threads_while_waiting

    results, threads_while_waiting = asyncio.run(main())

    assert threads_while_waiting <= before  # not one thread per stream, nor one per ping
    assert ticks >= 10  # the loop went on running while they waited
    assert all(len(r) == 3 and r[2].startswith("id: 2\nevent: message.end\n") for r in results)


# ---- the hub -------------------------------------------------------------------------------------------------------


def test_a_finished_reply_is_forgotten_after_the_keep_time_but_a_running_one_never_is(monkeypatch):
    monkeypatch.setattr(hub, "_replies", {})
    done, running = hub.start(uuid4(), uuid4(), uuid4()), hub.start(uuid4(), uuid4(), uuid4())
    done.finish()
    assert hub.HUB_KEEP_SECONDS == 300 and hub.get(done.message_id) is done  # kept for a reconnect
    assert hub.running() == [running] and hub.is_running(running.message_id) and not hub.is_running(done.message_id)

    monkeypatch.setattr(hub, "HUB_KEEP_SECONDS", 0)

    assert hub.get(done.message_id) is None and hub.get(running.message_id) is running


def test_event_ids_count_from_one_and_since_returns_only_those_above(monkeypatch):
    monkeypatch.setattr(hub, "_replies", {})
    reply = hub.start(uuid4(), uuid4(), uuid4())
    for n in range(3):
        reply.emit("text.delta", {"text": str(n)})

    assert [e.id for e in reply.since(0)] == [1, 2, 3, 4] and reply.last_id == 4
    assert [e.id for e in reply.since(2)] == [3, 4] and reply.since(4) == [] and reply.since(-5) == reply.since(0)
    assert reply.snapshot() == (4, {"text": "012", "citations": [], "proposals": []})
    end_of(reply)
    assert reply.snapshot()[0] == 4  # a message.end is not part of a snapshot: the stream still sends it
