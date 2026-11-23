"""The chat turn loop (plan 05-03; AI_SYSTEM 5.1 to 5.4, 9; CHT-01 to CHT-04): one candidate message in, one reply out.

A LangGraph graph of two nodes, `model` and `tools`, compiled once with `checkpointer=False`, so it runs like a plain
function and keeps nothing: the messages live in memory for the turn, and whoever called it stores the results. Models
are reached only through app.platform.llm.complete (never a LangChain model class), and each turn the model answers with
a validated ChatStep: tool calls to run, or the final reply (decision 1: structured output, not native tool calling).

    model   checks the limits and the stop flag, then asks the model for a step
    tools   runs the step's calls through the tools it was given; each result goes back to the model inside the
            untrusted envelope (refs.wrap), and its ids become aliases the model may cite

Limits for one candidate message, all assumptions (AI_SYSTEM 5.3): 6 model turns, 8 tool calls, 45 seconds, 40,000
tokens. Over any of them the turn ends with finish_reason "length" and what was written; a stop ends it with
"stopped"; a tool that raises ends it with "interrupted". The gateway's AIDisabled, ProviderUnavailable and
SchemaInvalid are not caught: the caller maps them. Nothing here writes to the database.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.platform import log
from app.platform.llm import complete

from . import refs
from .refs import AliasTable, Citation
from .schemas import ChatStep, ProposalEvent, ToolOutput

TASK = "chat.turn"
PROMPT = (Path(__file__).parent / "prompts" / "chat_turn.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)

MAX_MODEL_TURNS = 6  # assumption: AI_SYSTEM 5.3
MAX_TOOL_CALLS = 8  # assumption
MAX_SECONDS = 45  # assumption
MAX_TOKENS = 40_000  # assumption
# LangGraph counts every node visit as a step. A reply that reaches the turn limit makes seven `model` visits (the
# seventh only ends it) and six `tools` visits, 13 visits, and the graph needs one more step than it visits. So the
# limit sits well above the turn limit, or GraphRecursionError would end a turn before it could say "length"
# (decision 34).
RECURSION_LIMIT = 2 * MAX_MODEL_TURNS + 4

TODAY_LINE = "Today is {date}."  # lines the app puts before the candidate's message; the prompt describes both
CONTEXT_JOB_LINE = "The candidate has the job {alias} open on screen."

Emit = Callable[[str, dict[str, Any]], None]
Tool = Callable[[UUID, Any, AliasTable], ToolOutput]


@dataclass(frozen=True)
class TurnContext:
    """One candidate message. `history` is the earlier user and assistant messages, oldest first, as {role, content}.
    `aliases` is the session's alias table: the loop adds what tools return to it. `context_job_id` is the job page the
    candidate has open, if any. `today` is the candidate's date (UTC when not given)."""

    user_id: UUID
    user_text: str
    history: Sequence[Mapping[str, str]]
    aliases: AliasTable
    context_job_id: int | None = None
    today: date | None = None


@dataclass(frozen=True)
class TurnResult:
    """How a turn ended. `text` is the reply as it is stored and shown (known [[alias]] markers kept, others plain), and
    is empty unless finish_reason is "complete". `proposals` are the record changes proposed along the way.
    `unknown_citations` is how many [[markers]] named an alias no tool returned and were rewritten to plain text (the
    service stores the count); it is not part of what a result equals."""

    text: str
    finish_reason: str  # complete | length | stopped | interrupted
    citations: tuple[Citation, ...]
    proposals: tuple[ProposalEvent, ...]
    unknown_citations: int = field(default=0, compare=False)


class _State(TypedDict):
    messages: list[dict[str, str]]  # what the model is sent next
    turns: int  # model turns taken
    calls: int  # tool calls run
    pending: list[Any]  # the tool calls of the last step, not yet run
    reply: str | None  # the final reply, as the model wrote it
    finish: str | None  # why the turn ended, unless it ended with a reply
    proposals: list[ProposalEvent]


@dataclass
class _Run:
    """What one run needs besides its state: what the caller passes in. It reaches the nodes as the graph's context."""

    ctx: TurnContext
    tools: Mapping[str, Tool]
    emit: Emit
    should_stop: Callable[[], bool]
    tokens_used: Callable[[], int]
    started: float


def _now() -> float:
    return time.monotonic()


def _halt(run: _Run, *, before_model: bool) -> str | None:
    """A reason to end the turn now: the stop flag, or a limit. Tokens are looked at before a model turn only, because
    only model calls spend them."""
    if run.should_stop():
        return "stopped"
    if _now() - run.started >= MAX_SECONDS or (before_model and run.tokens_used() >= MAX_TOKENS):
        return "length"
    return None


def _opening(ctx: TurnContext) -> list[dict[str, str]]:
    """The prompt, the earlier messages, then the candidate's message with the lines that come from the app."""
    today = ctx.today or datetime.now(timezone.utc).date()
    lines = [TODAY_LINE.format(date=today.isoformat())]
    if ctx.context_job_id is not None:
        lines.append(CONTEXT_JOB_LINE.format(alias=ctx.aliases.add("job", ctx.context_job_id)))
    earlier = [
        {"role": m["role"], "content": m["content"]}
        for m in ctx.history
        if m["role"] in ("user", "assistant") and m["content"].strip()
    ]
    while earlier and earlier[0]["role"] != "user":
        earlier.pop(0)  # a conversation starts with the person
    return [
        {"role": "system", "content": PROMPT},
        *earlier,
        {"role": "user", "content": "\n".join(lines) + "\n\n" + ctx.user_text},
    ]


def _model(state: _State, runtime: Runtime[_Run]) -> dict[str, Any]:
    run = runtime.context
    reason = _halt(run, before_model=True) or ("length" if state["turns"] >= MAX_MODEL_TURNS else None)
    if reason is not None:
        return {"finish": reason, "pending": []}
    step = cast(
        ChatStep,
        complete(
            TASK, ChatStep, state["messages"], principal=run.ctx.user_id, prompt_version=PROMPT_VERSION, ledger="api"
        ),
    )
    written = {"role": "assistant", "content": step.model_dump_json(exclude_none=True)}
    return {
        "messages": [*state["messages"], written],
        "turns": state["turns"] + 1,
        "pending": list(step.tool_calls),
        "reply": None if step.tool_calls else step.reply,
    }


def _after_model(state: _State) -> str:
    return "tools" if state["pending"] else END


def _run_tool(call: Any, number: int, run: _Run) -> ToolOutput:
    """One call: announce it, run the tool as the session's person, give the ids it returned their aliases, and report
    them. The events carry ids only, never what the tool read."""
    name, call_id = call.tool, f"call_{number}"
    run.emit("tool.start", {"call_id": call_id, "tool": name})
    output = run.tools[name](run.ctx.user_id, call, run.ctx.aliases)
    aliases = {}
    for kind, ids in output.refs.items():
        for ref in ids:
            aliases[run.ctx.aliases.add(kind, ref)] = {"kind": kind, "id": str(ref)}
    by_kind = {f"{kind}_ids": ids for kind, ids in output.refs.items()}
    run.emit("tool.result", {"call_id": call_id, "tool": name, "refs": by_kind, "aliases": aliases})
    for proposal in output.proposals:
        run.emit("proposal", {**asdict(proposal), "event_id": str(proposal.event_id)})
    return output


def _tools(state: _State, runtime: Runtime[_Run]) -> dict[str, Any]:
    run = runtime.context
    calls, results, proposals = state["calls"], [], list(state["proposals"])
    finish = None
    for call in state["pending"]:
        finish = _halt(run, before_model=False) or ("length" if calls >= MAX_TOOL_CALLS else None)
        if finish is not None:
            break
        calls += 1
        try:
            output = _run_tool(call, calls, run)
        except Exception as error:  # a tool that fails ends the turn: what was written is kept, nothing is retried
            log.event("chat.tool_failed", level=logging.ERROR, kind=call.tool, error=error)
            finish = "interrupted"
            break
        proposals += output.proposals
        data = json.dumps(output.data, ensure_ascii=False, default=str)
        results.append(refs.wrap(output.source_kind, f"call_{calls}", data, untrusted=output.untrusted))
    messages = state["messages"]
    if finish is None:  # the turn goes on: the model gets the results; otherwise nobody will read them
        content = "Tool results, in the order you called them:\n" + "\n".join(results)
        messages = [*messages, {"role": "user", "content": content}]
    return {"messages": messages, "calls": calls, "pending": [], "proposals": proposals, "finish": finish}


def _after_tools(state: _State) -> str:
    return END if state["finish"] else "model"


def _build() -> Any:
    graph = StateGraph(_State, context_schema=_Run)
    graph.add_node("model", _model)
    graph.add_node("tools", _tools)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", _after_model, ["tools", END])
    graph.add_conditional_edges("tools", _after_tools, ["model", END])
    return graph.compile(checkpointer=False)  # no checkpoint: a turn is a function call, and only its results are kept


GRAPH = _build()


def _sentences(text: str) -> list[str]:
    """The text in sentence-sized pieces that join back to it exactly (white space stays with the next piece).

    ponytail: a break after every . ! or ? that a space follows, so "e.g. this" is two pieces; the pieces still join
    to the text, and the panel announces the reply once it is complete, so a wrong break is never seen."""
    return [piece for piece in re.split(r"(?<=[.!?])(?=\s)", text) if piece]


NOT_DONE_NOTE = (
    "Note: I cannot apply, send messages, approve anything or change your record. Nothing was changed; a change to your "
    "record waits for your confirmation."
)
# The false-claim check (05-FIXES B1). The agent has seven read and propose tools and nothing that applies, sends,
# approves or changes the record, so no event of a turn can ever back a claim that one of those was done: a reply that
# says so is false by construction. This is a KEYWORD FLOOR with known misses (a claim worded without these verbs, in
# another language, or split across sentences gets through), not a classifier. A sentence that says it did NOT, or that
# it cannot, is left alone.
_DONE = (
    r"(?:applied|submitted|sent|emailed|e-mailed|mailed|messaged|forwarded|approved|confirmed|recorded|saved|stored|"
    r"added|updated|changed|edited|removed|deleted|lowered|raised|set)"
)
# "your hard constraints were applied to this search" is true (a search always applies them), so the passive leaves
# "applied" out; "I applied" is still a claim.
_DONE_PASSIVE = _DONE.replace("applied|", "")
_BEFORE_VERB = r"(?:'ve|\s+have|\s+had|\s+just|\s+already|\s+also|\s+now|\s+successfully|\s+went\s+ahead\s+and)*"
_CLAIMS = (
    re.compile(rf"\b(?:I|we){_BEFORE_VERB}\s+{_DONE}\b", re.IGNORECASE),  # "I have applied", "I added the skill"
    # "your application was approved and sent", "your location constraint has been removed", "it was submitted"
    re.compile(
        rf"\b(?:your\s+[\w'-]+(?:\s+[\w'-]+){{0,3}}|it|this|that|they)\s+(?:was|were|has\s+been|have\s+been)"
        rf"\s+(?:\w+\s+and\s+)?{_DONE_PASSIVE}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byour\s+[\w'-]+(?:\s+[\w'-]+){0,3}\s+(?:is|are)\s+now\b", re.IGNORECASE
    ),  # "your salary target is now 0"
    re.compile(r"^\W*done\b\s*[:!.]", re.IGNORECASE),  # "Done: ..."
)
_DENIAL = re.compile(r"\b(?:not|never|no|nothing|cannot|unable|without)\b|n't|\bcan\s*not\b", re.IGNORECASE)


def claims_done(reply: str) -> bool:
    """True when the reply says, in a sentence that does not deny it, that something was applied, sent, approved or
    changed (see the floor above)."""
    return any(
        not _DENIAL.search(sentence) and any(claim.search(sentence) for claim in _CLAIMS)
        for sentence in _sentences(reply)
    )


def checked_reply(reply: str) -> str:
    """The reply as it is stored and shown: when it claims an action the agent has no way to take, the fixed note is
    appended after it. The check is logged without any text."""
    if not claims_done(reply):
        return reply
    log.event("chat.false_claim_flagged")
    return f"{reply.rstrip()}\n\n{NOT_DONE_NOTE}"


def _cited(citation: Citation) -> dict[str, Any]:
    ref: str | int = int(citation.id) if citation.kind == "job" else citation.id  # a job's id is a number in the API
    return {"alias": citation.alias, "kind": citation.kind, f"{citation.kind}_id": ref}


def run_turn(
    ctx: TurnContext,
    tools: Mapping[str, Tool],
    *,
    emit: Emit,
    should_stop: Callable[[], bool],
    tokens_used: Callable[[], int],
) -> TurnResult:
    """Run one candidate message to its end. `tools` maps a tool name to a function (user_id, parsed input, alias table)
    that returns a ToolOutput; the user id is always ctx.user_id, never something the model wrote. `emit(event, data)`
    receives tool.start, tool.result, proposal, text.delta and citation as they happen (the service adds the rest).
    `should_stop()` is asked before every model turn and tool call; `tokens_used()` is the tokens this message has used
    so far, asked before every model turn."""
    run = _Run(ctx, tools, emit, should_stop, tokens_used, _now())
    first: _State = {
        "messages": _opening(ctx),
        "turns": 0,
        "calls": 0,
        "pending": [],
        "reply": None,
        "finish": None,
        "proposals": [],
    }
    final = cast(_State, GRAPH.invoke(first, {"recursion_limit": RECURSION_LIMIT}, context=run))
    finish = final["finish"] or "complete"
    log.event(
        "chat.turn",
        outcome=finish,
        model_turns=final["turns"],
        tool_calls=final["calls"],
        duration_ms=round((_now() - run.started) * 1000),
    )
    proposals = tuple(final["proposals"])
    if finish != "complete" or final["reply"] is None:
        return TurnResult("", finish, (), proposals)
    resolved = refs.resolve(final["reply"], ctx.aliases)
    words = checked_reply(resolved.text)  # one place: what is streamed is what is stored
    for piece in _sentences(words):
        emit("text.delta", {"text": piece})
    for citation in resolved.citations:
        emit("citation", _cited(citation))
    return TurnResult(words, "complete", resolved.citations, proposals, resolved.unknown)
