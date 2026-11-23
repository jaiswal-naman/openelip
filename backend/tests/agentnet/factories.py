"""Invented rows for the agent-network tests (09-04), on top of the 09-01 Twin factories.

    world = committed_world(engine)         # tests/twin/factories.twin_world, committed (the API opens its own
    connections)
    matches = shown_matches(conn, world.candidate)   # two shown decisions with evidence and one withheld one

Everything is made up ("Initech Demo", addresses on example.test). `conn` is a bootstrap connection (row-level security
does not apply to it), like the other factories.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from tests.campaigns.factories import make_action
from tests.campaigns.factories import make_run as make_agent_run
from tests.hiring.factories import Candidate
from tests.match.factories import (
    LOCAL_DATE,
    Job,
    Verdict,
    make_decision,
    make_job,
    make_run,
    make_shown_decision,
)
from tests.twin.factories import (
    Issued,
    TwinWorld,
    candidate_token,
    employer_question,
    org_token,
    twin_session,
    twin_world,
    withdraw,
)

__all__ = [
    "HEADERS",
    "META",
    "executed_action",
    "rpc",
    "twin_message",
    "Issued",
    "Matches",
    "TwinWorld",
    "candidate_token",
    "committed_world",
    "employer_question",
    "org_token",
    "shown_matches",
    "twin_session",
    "twin_world",
    "withdraw",
]

DESCRIPTION = "Initech Demo needs a data engineer. You know Python. You know SQL. Rust is a plus."
REQUIREMENTS = (("You know Python.", "required"), ("You know SQL.", "required"), ("Rust is a plus.", "preferred"))


@dataclass(frozen=True)
class Matches:
    run: UUID
    first: UUID  # a shown decision (job "Data engineer A")
    second: UUID  # a shown decision (job "Data engineer B")
    withheld: UUID  # a withheld decision, which no token may read
    jobs: tuple[Job, Job, Job]


def committed_world(engine: Engine) -> TwinWorld:
    with engine.begin() as conn:
        return twin_world(conn)


def shown_matches(conn: Connection, candidate: Candidate) -> Matches:
    """Two shown decisions (met, partly met, no evidence), the first a day before the second, and a withheld one."""
    facts = candidate.record.fact_ids
    jobs = tuple(
        make_job(conn, title=f"Data engineer {name}", description=DESCRIPTION, requirements=REQUIREMENTS)
        for name in "ABC"
    )
    earlier = make_run(conn, candidate.id, local_date=LOCAL_DATE - timedelta(days=1))
    run = make_run(conn, candidate.id, local_date=LOCAL_DATE)
    verdicts = [Verdict("met", (facts[1],)), Verdict("partly_met", (facts[2],)), Verdict("no_evidence")]
    first = make_shown_decision(conn, earlier, candidate.id, jobs[0], verdicts, rank_position=1)
    second = make_shown_decision(conn, run, candidate.id, jobs[1], verdicts, rank_position=2)
    withheld = make_decision(conn, run, candidate.id, jobs[2], outcome="withheld")
    return Matches(run, first, second, withheld, (jobs[0], jobs[1], jobs[2]))  # type: ignore[arg-type]


# ---- talking to /mcp as an outside agent ---------------------------------------------------------------------------

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
HEADERS = {
    "MCP-Protocol-Version": "2026-07-28",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(
    client: Any,
    secret: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    headers: dict[str, str] | None = None,
    request_id: int = 1,
) -> Any:
    """One JSON-RPC request in the 2026-07-28 form (headers and `_meta`), as an agent sends it. `name` sets Mcp-Name;
    for a
    tools/call it defaults to the tool in `params`."""
    body_params = {**(params or {}), "_meta": META}
    sent = {**HEADERS, "Mcp-Method": method}
    called = name or (params or {}).get("name")
    if method == "tools/call" and called:
        sent["Mcp-Name"] = called
    if secret is not None:
        sent["Authorization"] = f"Bearer {secret}"
    sent.update(headers or {})
    return client.post(
        "/mcp", headers=sent, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": body_params}
    )


def call(client: Any, secret: str, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    return rpc(client, secret, "tools/call", {"name": tool, "arguments": arguments or {}})


# ---- the Twin's side of a conversation, as the worker would write it (copied from tests/twin/test_intake.py) -------


def twin_message(
    conn: Connection,
    world: TwinWorld,
    session: UUID,
    question: UUID,
    kind: str,
    *,
    topic: str = "skills",
    body: str = "Invented.",
    cited: tuple[UUID, ...] = (),
    action: int | None = None,
) -> UUID:
    message: UUID = conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                          cited_fact_ids, in_reply_to, agent_action_id)
               VALUES (:s, :u, :o, 'twin', :b, CAST(:t AS twin_topic), CAST(:k AS twin_answer_kind),
                       CAST(:c AS uuid[]), :q, :a) RETURNING id"""
        ),
        {
            "s": session,
            "u": world.candidate.id,
            "o": world.org.id,
            "b": body,
            "t": topic,
            "k": kind,
            "c": list(cited),
            "q": question,
            "a": action,
        },
    ).scalar_one()
    return message


def executed_action(conn: Connection, world: TwinWorld, question: UUID, kind: str = "answer") -> int:
    run = make_agent_run(conn, world.candidate.id, None, "twin")
    return make_action(
        conn,
        run,
        action_type="twin_answer",
        status="executed",
        payload={"kind": kind, "question_id": str(question)},
        policy_decision="ask",
    )
