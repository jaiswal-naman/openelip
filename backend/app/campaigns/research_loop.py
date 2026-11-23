"""The Researcher's loop (CMP-02; AI_SYSTEM 6.4; INDEX decision 15), the one agent loop of the campaign graph.

A small LangGraph graph compiled with `checkpointer=False`, so it runs like a plain function and writes nothing to
schema `langgraph` (ARCHITECTURE 5.9 rule 1): a planner node asks `research.plan` for one tool call, a tool node carries
it out, and the two alternate until the planner submits or a limit is reached. Messages and pages live in memory; the
caller stores only the result.

The Dual LLM split of crew.research holds: the planner never sees page text; `read_page` hands one page to the reader,
which has no tools and sees nothing of the candidate; a claim is kept only when its quote is an exact substring of the
fetched page; every fetch, including a refused one, is reported through `on_tool_call` (and the node records it).
Everything the planner asks for passes `research.is_allowed` first, so an injected link cannot widen the allowlist.
"""

import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from app.platform.http import FetchFailed, UnsafeUrl
from app.platform.llm import SchemaInvalid, complete

from .crew import research

MAX_URL_RECORDED = 300  # assumption: characters of a requested URL kept in a record


@dataclass(frozen=True)
class ResearchJob:
    """What one run needs: the company, the hosts it may fetch from, the posting's address and where pages come from."""

    company_name: str
    hosts: frozenset[str]
    posting_url: str | None
    source: research.PageSource


@dataclass(frozen=True)
class ToolRecord:
    """One fetch: the address asked for, the content hash when a page came back, when, and the outcome (`ok` or a code:
    `off_allowlist`, `duplicate`, `not_found`, `timeout`, ...)."""

    tool: str
    url: str
    content_sha256: str | None
    at: datetime
    outcome: str


@dataclass
class ResearchResult:
    pages: list[research.FetchedPage]
    claims: list[dict[str, Any]]  # research.build_dossier's claims
    incomplete: bool  # a limit stopped the run before the planner submitted
    failures: list[tuple[str, str]]  # (url, code)
    stop: str  # submitted, turns, tool_calls, seconds, tokens or plan_invalid


@dataclass
class _Run:
    job: ResearchJob
    principal: UUID | None
    on_tool_call: Callable[[ToolRecord], None]
    tokens: Callable[[], int]
    started: float = field(default_factory=time.monotonic)
    turns: int = 0
    calls: int = 0
    pages: dict[str, research.FetchedPage] = field(default_factory=dict)
    flagged: dict[str, bool] = field(default_factory=dict)
    kept: list[research.KeptClaim] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    stop: str = ""


class _State(TypedDict):
    step: research.PlanStep | None
    done: bool


def _limit(run: _Run) -> str | None:
    if run.turns >= research.MAX_TURNS:
        return "turns"
    if run.calls >= research.MAX_TOOL_CALLS:
        return "tool_calls"
    if time.monotonic() - run.started >= research.MAX_SECONDS:
        return "seconds"
    if run.tokens() >= research.MAX_TOKENS:
        return "tokens"
    return None


def _planner(run: _Run) -> Callable[[_State], dict[str, Any]]:
    def plan(state: _State) -> dict[str, Any]:
        stop = _limit(run)
        if stop is not None:
            run.stop = stop
            return {"step": None, "done": True}
        run.turns += 1
        shown = [(page_id, page, run.flagged.get(page_id, False)) for page_id, page in run.pages.items()]
        messages = research.plan_messages(
            run.job.company_name,
            run.job.hosts,
            shown,
            run.kept,
            links=run.links,
            turns_left=research.MAX_TURNS - run.turns + 1,
        )
        try:
            step = complete(
                research.PLAN_TASK,
                research.PlanStep,
                messages,
                principal=run.principal,
                prompt_version=research.PLAN_VERSION,
            )
        except SchemaInvalid:
            run.failures.append(("", "plan_invalid"))
            run.stop = "plan_invalid"
            return {"step": None, "done": True}
        assert isinstance(step, research.PlanStep)
        if step.tool == "submit_result":
            run.stop = "submitted"
            return {"step": None, "done": True}
        return {"step": step, "done": False}

    return plan


def _record(run: _Run, url: str, page: research.FetchedPage | None, outcome: str) -> None:
    run.on_tool_call(
        ToolRecord(
            "fetch_public_page",
            url[:MAX_URL_RECORDED],
            None if page is None else page.content_sha256,
            datetime.now(UTC),
            outcome,
        )
    )


def _fetch(run: _Run, url: str) -> None:
    if not research.is_allowed(url, run.job.hosts):
        run.failures.append((url[:MAX_URL_RECORDED], "off_allowlist"))
        _record(run, url, None, "off_allowlist")  # the planner's request is refused and recorded, never made
        return
    if any(page.url == url for page in run.pages.values()):
        _record(run, url, None, "duplicate")
        return
    try:
        page = run.job.source(url)
    except (UnsafeUrl, FetchFailed) as failure:
        run.failures.append((url[:MAX_URL_RECORDED], failure.reason))
        _record(run, url, None, failure.reason)
        return
    run.pages[f"p{len(run.pages) + 1}"] = page
    _record(run, url, page, "ok")


def _read(run: _Run, page_id: str, questions: list[str]) -> None:
    page = run.pages.get(page_id)
    if page is None:
        run.failures.append((page_id, "unknown_page"))
        return
    try:
        answer = research.read_page(page, questions, principal=run.principal)
    except SchemaInvalid:
        run.failures.append((page.url, "read_invalid"))
        return
    kept, _dropped = research.keep_exact(page, answer)
    run.kept.extend(kept)
    run.flagged[page_id] = run.flagged.get(page_id, False) or answer.injection_suspected
    for link in research.allowed_links(answer.links, page.url, run.job.hosts):
        if link not in run.links:
            run.links.append(link)


def _tools(run: _Run) -> Callable[[_State], dict[str, Any]]:
    def act(state: _State) -> dict[str, Any]:
        step = state["step"]
        assert step is not None
        run.calls += 1
        if step.tool == "fetch_public_page":
            _fetch(run, step.url or "")
        elif step.tool == "read_page":
            _read(run, step.page_id or "", step.questions or [])
        return {"step": None, "done": False}

    return act


def _build(run: _Run) -> Any:
    graph = StateGraph(_State)
    graph.add_node("plan", _planner(run))  # type: ignore[call-overload]
    graph.add_node("tools", _tools(run))  # type: ignore[call-overload]
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", lambda s: END if s["done"] else "tools", ["tools", END])
    graph.add_edge("tools", "plan")
    return graph.compile(checkpointer=False)


def run(
    job: ResearchJob,
    *,
    principal: UUID | None,
    on_tool_call: Callable[[ToolRecord], None],
    tokens: Callable[[], int] = lambda: 0,
) -> ResearchResult:
    """Run the loop. `tokens` says how many model tokens this run has used so far (the node reads the ledger).
    Raises what the gateway raises for a disabled or unavailable provider (the queue retries the latter)."""
    state = _Run(job, principal, on_tool_call, tokens)
    # durability "exit": the loop has no checkpointer, and the library fails a "sync" it inherits from the campaign
    # graph (it then warns that the setting has no effect, which is the point: nothing is written)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="`durability` has no effect")
        _build(state).invoke(
            {"step": None, "done": False}, {"recursion_limit": 2 * research.MAX_TURNS + 10}, durability="exit"
        )
    claims = research.build_dossier(list(state.pages.values()), state.kept)
    return ResearchResult(
        pages=list(state.pages.values()),
        claims=claims,
        incomplete=state.stop not in ("submitted",),
        failures=state.failures,
        stop=state.stop,
    )
