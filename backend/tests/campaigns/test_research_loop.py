"""The Researcher loop (plan 07-04, CMP-02): a loop with no checkpointer that fetches only allowlisted pages, keeps only
exact quotes, flags injections, stops inside its limits and reports every fetch. Pages come from the replay directory
and
the planner and reader answers from recordings; everything is invented."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.campaigns import research_loop
from app.campaigns.crew import research
from app.platform.llm import fake
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401

HOSTS = research.allowed_hosts(w.APPLY_URL, w.APPLY_URL)
ABOUT = "https://careers.lumen.example/about"
CAREERS = "https://careers.lumen.example/careers"
PRODUCT = "Lumen Freight builds route planning software for regional trucking companies."


@pytest.fixture(autouse=True)
def graph_world(clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch)
    yield
    fake.replay_from(None)


def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan: list[Any], read: list[Any] | None = None) -> None:
    directory = tmp_path / "recorded"
    directory.mkdir()
    (directory / "research.plan.json").write_text(json.dumps(plan), encoding="utf-8")
    if read is not None:
        (directory / "research.read_page.json").write_text(json.dumps(read), encoding="utf-8")
    monkeypatch.setenv("LLM_FAKE_DIR", str(directory))
    fake.replay_from(None)


def go(**kwargs: Any) -> tuple[research_loop.ResearchResult, list[research_loop.ToolRecord], list[str]]:
    fetched: list[str] = []
    source = research.replay_source(w.PAGES, HOSTS)

    def counting(url: str) -> research.FetchedPage:
        fetched.append(url)
        return source(url)

    seen: list[research_loop.ToolRecord] = []
    job = research_loop.ResearchJob("Lumen Freight", HOSTS, w.APPLY_URL, counting)
    result = research_loop.run(job, principal=None, on_tool_call=seen.append, **kwargs)
    return result, seen, fetched


def test_the_recorded_run_builds_a_cited_dossier_and_reports_each_fetch() -> None:
    result, seen, fetched = go()
    assert not result.incomplete and result.stop == "submitted"
    assert fetched == [ABOUT, CAREERS]
    assert [r.outcome for r in seen] == ["ok", "ok"]
    assert all(r.content_sha256 and len(r.content_sha256) == 64 and r.at for r in seen)
    assert [r.url for r in seen] == [ABOUT, CAREERS]
    assert [c["topic"] for c in result.claims] == ["product", "size", "hiring_process"]
    assert all(c["sources"][0]["url"] for c in result.claims)
    page_text = {p.url: p.text for p in result.pages}
    assert all(c["sources"][0]["quote"] in page_text[c["sources"][0]["url"]] for c in result.claims)


def test_a_request_for_another_host_is_refused_and_recorded_never_fetched(tmp_path, monkeypatch) -> None:
    record(
        tmp_path,
        monkeypatch,
        [
            {"tool": "fetch_public_page", "url": "https://evil.invalid/steal"},
            {"tool": "fetch_public_page", "url": "http://careers.lumen.example/about"},  # a downgrade
            {"tool": "fetch_public_page", "url": ABOUT},
            {"tool": "submit_result"},
        ],
    )
    result, seen, fetched = go()
    assert fetched == [ABOUT]
    assert [(r.url, r.outcome) for r in seen] == [
        ("https://evil.invalid/steal", "off_allowlist"),
        ("http://careers.lumen.example/about", "off_allowlist"),
        (ABOUT, "ok"),
    ]
    assert seen[0].content_sha256 is None
    assert ("https://evil.invalid/steal", "off_allowlist") in result.failures


def test_a_quote_that_is_not_on_the_page_is_dropped(tmp_path, monkeypatch) -> None:
    record(
        tmp_path,
        monkeypatch,
        [
            {"tool": "fetch_public_page", "url": ABOUT},
            {"tool": "read_page", "page_id": "p1", "questions": ["What do they make?"]},
            {"tool": "submit_result"},
        ],
        [
            {
                "claims": [
                    {"topic": "product", "quote": PRODUCT},
                    {"topic": "size", "quote": "The company has 850 employees."},  # a changed number
                    {"topic": "values", "quote": "They value people very much."},  # a paraphrase
                    {"topic": "news", "quote": ""},
                ],
                "links": [],
                "injection_suspected": False,
            }
        ],
    )
    result, _, _ = go()
    assert [c["text"] for c in result.claims] == [PRODUCT]


def test_the_injection_flag_travels_and_the_planner_never_sees_page_text(tmp_path, monkeypatch) -> None:
    record(
        tmp_path,
        monkeypatch,
        [
            {"tool": "fetch_public_page", "url": ABOUT},
            {"tool": "read_page", "page_id": "p1", "questions": ["What do they make?"]},
            {"tool": "submit_result"},
        ],
        [
            {
                "claims": [{"topic": "product", "quote": PRODUCT}],
                "links": ["https://evil.invalid/x"],
                "injection_suspected": True,
            }
        ],
    )
    sent: list[list[dict[str, str]]] = []
    real = research_loop.complete

    def spy(task, schema, messages, **kwargs):  # type: ignore[no-untyped-def]
        if task == research.PLAN_TASK:
            sent.append(messages)
        return real(task, schema, messages, **kwargs)

    monkeypatch.setattr(research_loop, "complete", spy)
    result, _, _ = go()
    assert result.claims[0]["injection_suspected"] is True
    everything = "\n".join(m["content"] for messages in sent for m in messages)
    assert PRODUCT not in everything and "85 employees" not in everything  # page text never reaches the planner
    assert "evil.invalid" not in everything  # an off-list link is not offered
    assert "[injection suspected]" in everything  # the flag is


@pytest.mark.parametrize(
    ("limit", "value", "stop"),
    [("MAX_TURNS", 2, "turns"), ("MAX_TOOL_CALLS", 1, "tool_calls"), ("MAX_SECONDS", 0, "seconds")],
)
def test_a_limit_stops_the_loop_with_what_it_has(limit, value, stop, monkeypatch) -> None:
    monkeypatch.setattr(research, limit, value)
    result, _, _ = go()
    assert result.incomplete and result.stop == stop
    assert len(result.pages) <= 2


def test_the_token_limit_stops_the_loop(monkeypatch) -> None:
    monkeypatch.setattr(research, "MAX_TOKENS", 10)
    result, _, fetched = go(tokens=lambda: 11)
    assert result.incomplete and result.stop == "tokens" and fetched == []


def test_a_planner_answer_that_stays_invalid_ends_the_run_incomplete(tmp_path, monkeypatch) -> None:
    record(tmp_path, monkeypatch, ["not json", "still not json"])
    result, _, _ = go()
    assert result.incomplete and result.stop == "plan_invalid" and ("", "plan_invalid") in result.failures


def test_a_page_that_was_not_recorded_is_a_failure_not_a_guess(tmp_path, monkeypatch) -> None:
    record(
        tmp_path,
        monkeypatch,
        [{"tool": "fetch_public_page", "url": "https://careers.lumen.example/missing"}, {"tool": "submit_result"}],
    )
    result, seen, _ = go()
    assert [r.outcome for r in seen] == ["not_found"]
    assert result.claims == [] and not result.incomplete


def test_the_loop_writes_nothing_to_the_checkpoint_schema(engine) -> None:
    from sqlalchemy import text

    go()
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM langgraph.checkpoints")).scalar_one() == 0
