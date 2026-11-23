"""The Twin graph's nodes (ARCHITECTURE 4.6 and 5.9; AI_SYSTEM 8.3 to 8.6; INDEX "Thread, state and queue").

One thread per employer question, `twin:{question_id}`, whose state is four short values and nothing else (the
question's id, the next node, the resume cause and the action it is parked on): the question, the facts, the draft and
the reply live in tables and are read again by each node. Every node opens its own worker transaction as the
candidate, does one thing, and checks first whether that thing is already done, so a node that runs again after a crash
between its commit and its checkpoint writes nothing twice; `twin_messages_one_final` is the last guard.

    triage         restricted patterns, then twin.classify; decline, draft or ask the candidate
    draft          twin.answer over the open facts, then the proposal of an answer (and what the Guardian says of it)
    ask_candidate  the holding message "the record does not answer this"
    propose        the relay proposal that waits for the candidate's own reply
    park           interrupt on the approval; the resume value is a cause id, never text
    finish         acts on the parked action (or its edited replacement) and ends the thread

Nothing here lets a message reach the employer except `deliver.say` (declined and holding messages, fixed texts) and
`deliver.post` (an executed, Guardian-checked `twin_answer`).
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, TypedDict
from uuid import UUID

from langgraph.types import interrupt
from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.platform import db, log
from app.platform.llm import AIDisabled, SchemaInvalid, attributed, complete
from app.profile import api as profile
from app.trust import api as trust

from . import api as twin
from . import deliver, logic
from .schemas import TopicClassification, TwinAnswer

END = "end"
GRAPH_VERSION = "twin-graph-1"
RUN_ROLE = "twin"
CAUSE = re.compile(r"a(\d+)\.([a-z_]+)")
NEGATIVE = ("rejected", "expired", "cancelled", "blocked", "failed")


class State(TypedDict):
    question_id: str
    next: str | None
    cause: str | None
    parked_on: int | None


@dataclass(frozen=True)
class Question:
    id: UUID
    session_id: UUID
    user_id: UUID
    organization_id: UUID
    introduction_id: UUID
    body: str


class Gone(Exception):
    """The question is no longer there (its candidate was erased): the thread ends."""


def thread_id(question_id: UUID | str) -> str:
    return deliver.thread_id(question_id)


@contextmanager
def _open(question_id: str) -> Iterator[tuple[Connection, Question, UUID]]:
    """A worker transaction acting for the question's candidate, the question and its agent run. The run is made (and
    committed) first, in a transaction of its own: the gateway writes its ledger rows on another connection, and their
    foreign key to the run needs the run to be visible."""
    with db.transaction(role="worker") as conn:
        row = conn.execute(
            text(
                """SELECT m.id, m.session_id, m.user_id, m.organization_id, m.body, s.introduction_id
                     FROM twin_messages m JOIN twin_sessions s ON s.id = m.session_id
                    WHERE m.id = :q AND m.author = 'employer'"""
            ),
            {"q": UUID(question_id)},
        ).one_or_none()
    if row is None:
        raise Gone
    question = Question(row.id, row.session_id, row.user_id, row.organization_id, row.introduction_id, row.body)
    with db.transaction(question.user_id, role="worker") as conn:
        run_id = _run_of(conn, question)
    with db.transaction(question.user_id, role="worker") as conn:
        yield conn, question, run_id


def _gone(node: Any) -> Any:
    """A node whose question has been erased ends its thread."""

    def run(state: State) -> dict[str, Any]:
        try:
            return node(state)  # type: ignore[no-any-return]
        except Gone:
            return {"next": END, "cause": None}

    run.__name__ = node.__name__
    return run


# ---- the run and what a node remembers -----------------------------------------------------------------------------


def _run_of(conn: Connection, q: Question) -> UUID:
    """The question's agent run, made on first use. Its first recorder row names the question, so it is found again."""
    find = text(
        """SELECT r.id FROM agent_runs r JOIN agent_actions a ON a.agent_run_id = r.id
            WHERE r.twin_session_id = :s AND r.agent_role = 'twin' AND a.kind = 'tool_call'
              AND a.inputs ->> 'stage' = 'question' AND a.inputs ->> 'question_id' = :q
            ORDER BY r.started_at LIMIT 1"""
    )
    args = {"s": q.session_id, "q": str(q.id)}
    found = conn.execute(find, args).scalar_one_or_none()
    if found is not None:
        return found  # type: ignore[no-any-return]
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"twin.run:{q.id}"})
    found = conn.execute(find, args).scalar_one_or_none()
    if found is not None:
        return found  # type: ignore[no-any-return]
    run_id: UUID = conn.execute(
        text(
            """INSERT INTO agent_runs (user_id, agent_role, config_version, twin_session_id)
               VALUES (:u, CAST(:role AS agent_role), :v, :s) RETURNING id"""
        ),
        {"u": q.user_id, "role": RUN_ROLE, "v": GRAPH_VERSION, "s": q.session_id},
    ).scalar_one()
    guardian.record(
        conn,
        run_id,
        kind="tool_call",
        summary="Twin question received",
        inputs={"stage": "question", "question_id": str(q.id)},
    )
    return run_id


def _remember(conn: Connection, run_id: UUID, q: Question, **facts: Any) -> None:
    guardian.record(
        conn, run_id, kind="tool_call", summary=f"Twin {facts['stage']}", inputs={"question_id": str(q.id), **facts}
    )


def _triage_of(conn: Connection, run_id: UUID) -> dict[str, Any] | None:
    """What triage decided for this run (the topic and the route), or None before it has."""
    found = conn.execute(
        text(
            """SELECT inputs FROM agent_actions WHERE agent_run_id = :r AND kind = 'tool_call'
                  AND inputs ->> 'stage' = 'triage' ORDER BY id LIMIT 1"""
        ),
        {"r": run_id},
    ).scalar_one_or_none()
    return None if found is None else dict(found)


def _action_of(conn: Connection, run_id: UUID, q: Question, kind: str) -> tuple[int, str] | None:
    """The question's `twin_answer` action of this kind that no edit replaced: its id and status."""
    row = conn.execute(
        text(
            """SELECT id, status::text AS status FROM agent_actions
                WHERE agent_run_id = :r AND action_type = 'twin_answer' AND inputs ->> 'question_id' = :q
                  AND inputs ->> 'kind' = :k AND inputs ->> 'replaces' IS NULL ORDER BY id LIMIT 1"""
        ),
        {"r": run_id, "q": str(q.id), "k": kind},
    ).one_or_none()
    return None if row is None else (row.id, row.status)


def _permissions(conn: Connection, q: Question) -> dict[str, str]:
    return {row.topic: row.permission for row in twin.get_scopes(conn, q.user_id)}


def _dial(conn: Connection, q: Question) -> str:
    for row in guardian.get_autonomy(conn, q.user_id).rows:
        if row.action_type == "twin_answer":
            return row.level
    return "ask_first"


def _payload(q: Question, topic: str, kind: str, validated: logic.Validated | None) -> dict[str, Any]:
    sentences: list[dict[str, Any]] = (
        []
        if validated is None
        else [{"text": s.text, "fact_ids": [str(i) for i in s.fact_ids]} for s in validated.sentences]
    )
    cited = sorted({str(fid) for s in sentences for fid in s["fact_ids"]})
    return {
        "kind": kind,
        "question_id": str(q.id),
        "session_id": str(q.session_id),
        "introduction_id": str(q.introduction_id),
        "organization_id": str(q.organization_id),
        "topic": topic,
        "body": " ".join(s["text"] for s in sentences) if kind == "answer" else None,
        "sentences": sentences,
        "cited_fact_ids": cited,
    }


# ---- ending ---------------------------------------------------------------------------------------------------------


def _end(conn: Connection, q: Question, run_id: UUID, status: str = "succeeded") -> dict[str, Any]:
    """The question is settled: close the run and ask for the thread's checkpoints to go."""
    guardian.close_run(conn, run_id, status=status)
    deliver.enqueue_delete(conn, q.id, q.user_id)
    return {"next": END, "cause": None, "parked_on": None}


def _decline(
    conn: Connection, q: Question, run_id: UUID, topic: str, body: str = logic.DECLINED_TEXT
) -> dict[str, Any]:
    deliver.say(
        conn,
        q.id,
        kind="declined",
        body=body,
        topic=topic,
        session_id=q.session_id,
        user_id=q.user_id,
        organization_id=q.organization_id,
    )
    return _end(conn, q, run_id)


def _hold(conn: Connection, q: Question, topic: str, body: str) -> None:
    deliver.say(
        conn,
        q.id,
        kind="unknown",
        body=body,
        topic=topic,
        session_id=q.session_id,
        user_id=q.user_id,
        organization_id=q.organization_id,
    )


# ---- triage ---------------------------------------------------------------------------------------------------------


def _classify(q: Question, run_id: UUID) -> TopicClassification | None:
    """twin.classify over the question and nothing else. A classifier that is off or answers invalidly twice is None,
    which `logic.merge` treats as asking for restricted data: nothing is answered on a guess. A provider that is down
    raises, so the queue tries again."""
    try:
        with attributed(campaign_id=None, agent_run_id=run_id):
            answer = complete(
                logic.CLASSIFY_TASK,
                TopicClassification,
                logic.classify_messages(q.body),
                principal=q.user_id,
                prompt_version=logic.CLASSIFY_PROMPT_VERSION,
            )
    except (AIDisabled, SchemaInvalid):
        return None
    assert isinstance(answer, TopicClassification)
    return answer


def _restricted_in_session(conn: Connection, q: Question) -> int:
    """Questions of this session already declined as restricted (pay or contact details)."""
    return int(
        conn.execute(
            text(
                """SELECT count(*) FROM twin_messages WHERE session_id = :s AND author = 'twin'
                      AND answer_kind = 'declined' AND topic IN ('current_pay', 'contact_details')"""
            ),
            {"s": q.session_id},
        ).scalar_one()
    )


@_gone
def triage(state: State) -> dict[str, Any]:
    with _open(state["question_id"]) as (conn, q, run_id):
        if deliver.has_final(conn, q.id):
            return {"next": END, "cause": None}
        if guardian.session_spent_microusd(conn, q.session_id) >= guardian.SESSION_COST_MICROUSD:
            result = _decline(conn, q, run_id, deliver.CLOSING_TOPIC, logic.LIMIT_TEXT)
            deliver.close_session(conn, q.session_id)
            return result
        decided = _triage_of(conn, run_id)
        if decided is None:
            patterns = trust.restricted_matches(q.body)
            classification = None if patterns else _classify(q, run_id)
            topic = logic.merge([m.topic for m in patterns], classification)
            permission = "never" if topic in logic.RESTRICTED_TOPICS else _permissions(conn, q).get(topic, "ask_me")
            route = logic.route(permission, _dial(conn, q), topic)
            _remember(
                conn,
                run_id,
                q,
                stage="triage",
                topic=topic,
                route=route.value,
                patterns=sorted({m.kind for m in patterns}),
            )
            decided = {"topic": topic, "route": route.value}
        topic, route = decided["topic"], logic.Route(decided["route"])
        if route is logic.Route.DECLINE:
            if topic in logic.RESTRICTED_TOPICS and _restricted_in_session(conn, q) + 1 >= logic.RESTRICTED_TO_CLOSE:
                result = _decline(conn, q, run_id, topic, logic.CLOSED_TEXT)
                deliver.close_session(conn, q.session_id)
                return result
            return _decline(conn, q, run_id, topic)
        return {"next": "draft" if route is logic.Route.DRAFT else "ask_candidate", "cause": None}


# ---- what the Guardian said of a proposal ---------------------------------------------------------------------------


def _settle(
    conn: Connection, q: Question, run_id: UUID, topic: str, action_id: int, status: str, *, relay_on_block: bool
) -> dict[str, Any]:
    """Act on an action in the state it is in: post a scheduled one, wait for an awaiting one, ask the candidate when a
    drafted answer was blocked, and decline when the candidate said no, the approval expired or a relay cannot go."""
    if status == "scheduled":
        if deliver.post(conn, action_id) or deliver.has_final(conn, q.id):
            return _end(conn, q, run_id)
        return _decline(conn, q, run_id, topic)
    if status == "awaiting_approval":
        if _is_answer(conn, action_id):
            _hold(conn, q, topic, logic.REVIEW_TEXT)
        return {"next": "park", "parked_on": action_id, "cause": None}
    if status == "blocked" and relay_on_block:
        return {"next": "ask_candidate", "cause": None}
    return _decline(conn, q, run_id, topic)


def _is_answer(conn: Connection, action_id: int) -> bool:
    kind = conn.execute(
        text("SELECT payload ->> 'kind' FROM agent_actions WHERE id = :a"), {"a": action_id}
    ).scalar_one_or_none()
    return kind == "answer"


def _propose(
    conn: Connection, q: Question, run_id: UUID, topic: str, kind: str, validated: logic.Validated | None
) -> Any:
    payload = _payload(q, topic, kind, validated)
    sentences = payload["sentences"]
    provenance: dict[str, Any] = {
        "fields": {"body": "model" if kind == "answer" else "candidate"},
        "sentences": [{"field": "body", "text": s["text"], "fact_ids": s["fact_ids"]} for s in sentences],
    }
    return guardian.propose_action(
        conn,
        run_id,
        action_type="twin_answer",
        payload=payload,
        provenance=provenance,
        organization_id=q.organization_id,
        inputs={"question_id": str(q.id), "kind": kind},
    )


# ---- draft ----------------------------------------------------------------------------------------------------------


def _validated(conn: Connection, q: Question, run_id: UUID, topic: str) -> logic.Validated | None:
    """The answer drafted from the open facts, after validation; None when nothing in the record answers (no open fact,
    the model says unknown or fails invalidly, or no sentence survives)."""
    facts = [
        SimpleNamespace(id=f["id"], kind=f["kind"], statement=f["statement"])
        for f in profile.usable_facts(conn, q.user_id)
    ]
    open_ = logic.open_facts(facts, _permissions(conn, q))
    if not open_:
        return None
    try:
        with attributed(campaign_id=None, agent_run_id=run_id):
            answer = complete(
                logic.ANSWER_TASK,
                TwinAnswer,
                logic.answer_messages(q.body, topic, open_),
                principal=q.user_id,
                prompt_version=logic.ANSWER_PROMPT_VERSION,
            )
    except (AIDisabled, SchemaInvalid):
        return None
    assert isinstance(answer, TwinAnswer)
    validated = logic.validate(answer, open_)
    _remember(conn, run_id, q, stage="draft", topic=topic, kind=validated.kind, dropped=validated.dropped)
    return validated if validated.kind == "answered" else None


@_gone
def draft(state: State) -> dict[str, Any]:
    with _open(state["question_id"]) as (conn, q, run_id):
        if deliver.has_final(conn, q.id):
            return {"next": END, "cause": None}
        topic = (_triage_of(conn, run_id) or {}).get("topic", "other")
        found = _action_of(conn, run_id, q, "answer")
        if found is None:
            validated = _validated(conn, q, run_id, topic)
            if validated is None:
                return {"next": "ask_candidate", "cause": None}
            proposed = _propose(conn, q, run_id, topic, "answer", validated)
            found = (proposed.action_id, proposed.status)
        return _settle(conn, q, run_id, topic, found[0], found[1], relay_on_block=True)


# ---- ask the candidate ----------------------------------------------------------------------------------------------


@_gone
def ask_candidate(state: State) -> dict[str, Any]:
    with _open(state["question_id"]) as (conn, q, run_id):
        if deliver.has_final(conn, q.id):
            return {"next": END, "cause": None}
        topic = (_triage_of(conn, run_id) or {}).get("topic", "other")
        _hold(conn, q, topic, logic.UNKNOWN_TEXT)
        return {"next": "propose", "cause": None}


@_gone
def propose(state: State) -> dict[str, Any]:
    with _open(state["question_id"]) as (conn, q, run_id):
        if deliver.has_final(conn, q.id):
            return {"next": END, "cause": None}
        topic = (_triage_of(conn, run_id) or {}).get("topic", "other")
        found = _action_of(conn, run_id, q, "relay")
        if found is None:
            proposed = _propose(conn, q, run_id, topic, "relay", None)
            found = (proposed.action_id, proposed.status)
        return _settle(conn, q, run_id, topic, found[0], found[1], relay_on_block=False)


# ---- park and finish ------------------------------------------------------------------------------------------------


def park(state: State) -> dict[str, Any]:
    """Wait for the candidate. Only this node calls `interrupt` (INDEX decision 37); the reads before it run again on
    resume, which is harmless. The resume value is `{"cause": ...}`: an id, never the reply."""
    with db.transaction(role="worker") as conn:
        approval = conn.execute(
            text("SELECT id FROM approvals WHERE agent_action_id = :a"), {"a": state["parked_on"]}
        ).scalar_one_or_none()
    value = interrupt({"waiting_for": "approval", "ref": None if approval is None else str(approval)})
    return {"cause": str((value or {}).get("cause")), "next": "finish"}


@_gone
def finish(state: State) -> dict[str, Any]:
    with _open(state["question_id"]) as (conn, q, run_id):
        if deliver.has_final(conn, q.id):
            return _end(conn, q, run_id)
        named = CAUSE.fullmatch(state.get("cause") or "")
        row = (
            None
            if named is None
            else conn.execute(
                text(
                    """SELECT id, status::text AS status, outcome_code, inputs ->> 'replaces' AS replaces
                         FROM agent_actions WHERE id = :a AND action_type = 'twin_answer'
                           AND inputs ->> 'question_id' = :q"""
                ),
                {"a": int(named.group(1)), "q": str(q.id)},
            ).one_or_none()
        )
        parked = state.get("parked_on")
        ours = row is not None and parked is not None and (row.id == parked or row.replaces == str(parked))
        if not ours or row is None or row.status in ("awaiting_approval", "proposed") or row.outcome_code == "edited":
            return {"next": "park", "cause": None}  # not about the action the thread waits on: wait again
        topic = (_triage_of(conn, run_id) or {}).get("topic", "other")
        if row.status in ("scheduled", *NEGATIVE):
            return _settle(conn, q, run_id, topic, row.id, row.status, relay_on_block=False)
        log.event("twin.finish_ignored", status=row.status)
        return {"next": "park", "cause": None}
