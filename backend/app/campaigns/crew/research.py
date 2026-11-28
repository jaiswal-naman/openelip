"""The Researcher's pieces (CMP-02; AI_SYSTEM 6.4): what may be fetched, how a page becomes text, what the page reader
may return, which claims are kept and how a dossier is assembled. Plain functions with no graph in them; the loop that
calls them is plan 07-04's.

The Dual LLM split holds here. The planner (research.plan) sees the company name, the allowed hosts, a list of the
pages fetched and the claims already kept, never page text. The reader (research.read_page) sees one page and the
questions, never anything about the candidate, and has no tools. A claim is kept only when its quote is an exact
substring of the page text that was fetched (invariant 2 as amended), so the reader can paraphrase nothing into the
dossier. Nothing here touches the database.
"""

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, cast
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, model_validator

from app.platform import http
from app.platform.http import FetchFailed, UnsafeUrl
from app.platform.llm import complete

PROMPTS = Path(__file__).parent.parent / "prompts"
PLAN_TASK, READ_TASK = "research.plan", "research.read_page"
PLAN_PROMPT = (PROMPTS / "research_plan.md").read_text(encoding="utf-8")
READ_PROMPT = (PROMPTS / "research_read_page.md").read_text(encoding="utf-8")
# The template's content hash (AI_SYSTEM 2.2), in the 12-character form the eval runner uses.
PLAN_VERSION = hashlib.sha256(PLAN_PROMPT.encode()).hexdigest()[:12]
READ_VERSION = hashlib.sha256(READ_PROMPT.encode()).hexdigest()[:12]

# Limits of one research run (AI_SYSTEM 6.4; assumptions). The loop of 07-04 stops at the first one it reaches and
# writes
# a dossier marked incomplete.
MAX_TURNS = 12  # assumption
MAX_TOOL_CALLS = 20  # assumption
MAX_SECONDS = 600  # assumption: the 10 minutes per run of AI_SYSTEM 2.2
MAX_TOKENS = 150_000  # assumption
MAX_PAGE_BYTES = 2 * 1024 * 1024  # assumption: 2 MB per page
FETCH_SECONDS = 15  # assumption: http.fetch_public's own budget; stated here because AI_SYSTEM 6.4 names it
MAX_QUOTE = 500  # assumption: a quote is a sentence or two, not a page
MAX_PAGE_CHARS = 40_000  # assumption: what the reader is sent of one page
MAX_TITLE = 80  # assumption: characters of a page title shown to the planner
MAX_QUESTIONS = 6  # assumption
FIXED_PATHS = ("/", "/about", "/careers")  # the baseline's list, besides the posting itself (decision 16)
REPLAY_TIME = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)  # a recorded page was "fetched" at this fixed time

Topic = Literal["product", "customers", "size", "funding", "engineering", "hiring_process", "values", "news", "other"]
TOPICS: tuple[str, ...] = (
    "product",
    "customers",
    "size",
    "funding",
    "engineering",
    "hiring_process",
    "values",
    "news",
    "other",
)
NOT_FOUND = "not found on the company's pages"


# ---- the fetch policy ----------------------------------------


def _host_of(url: str) -> str | None:
    """The lower-case host of a plain https or http URL with no userinfo, an IP literal or a stray port; else None."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None:
        return None
    if port not in (None, 80, 443) or re.fullmatch(r"[\d.]+|[0-9a-f:]*:[0-9a-f:]*", host):
        return None
    return host


def allowed_hosts(job_source_url: str | None, apply_url: str | None) -> frozenset[str]:
    """The only hosts a run may fetch from: the host of the job's source page and of its apply link (AI_SYSTEM 6.4:
    the company's own domain or the job board page; nothing else in v1). A URL that is not a plain web address gives
    nothing."""
    hosts = (_host_of(url) for url in (job_source_url, apply_url) if url)
    return frozenset(host for host in hosts if host is not None)


def is_allowed(url: str, hosts: frozenset[str]) -> bool:
    """May this URL be fetched? https only (an http link is a downgrade), a host on the list, no userinfo, no IP
    literal, no port other than the default."""
    parts = urlsplit(url)
    return parts.scheme == "https" and (_host_of(url) in hosts) and _host_of(url) is not None


# ---- a page as text ----------------------------------------

_SKIPPED = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK = frozenset(
    {"p", "div", "li", "ul", "ol", "br", "tr", "table", "section", "article", "header", "footer", "nav", "main"}
    | {"h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dt", "dd", "form", "aside"}
)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)


class _Text(HTMLParser):
    """Visible text only: script, style and hidden elements are dropped, so the same text is quoted and checked."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._hidden: list[str] = []  # tags of the open elements that are hidden or skipped
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if not self._hidden and tag in _BLOCK:
                self.parts.append("\n")
            return
        values = dict(attrs)
        hidden = (
            tag in _SKIPPED
            or "hidden" in values
            or (values.get("aria-hidden") or "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(values.get("style") or ""))
        )
        if self._hidden or hidden:
            self._hidden.append(tag)
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag == "title" and not self._hidden:
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._hidden:
            if tag in self._hidden:
                while self._hidden and self._hidden.pop() != tag:
                    pass
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._hidden:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(visible text, title) of an HTML page. Lines are trimmed, runs of blanks become one space and empty lines go."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line), " ".join(parser.title.split())


# ---- page sources ----------------------------------------


@dataclass(frozen=True)
class FetchedPage:
    url: str
    text: str
    retrieved_at: datetime
    content_sha256: str
    title: str = ""  # untrusted: shown to the planner shortened, with the page list


PageSource = Callable[[str], FetchedPage]  # url -> the page; raises UnsafeUrl or FetchFailed


def _page(url: str, text: str, title: str, retrieved_at: datetime) -> FetchedPage:
    return FetchedPage(url, text, retrieved_at, hashlib.sha256(text.encode()).hexdigest(), title)


def replay_source(directory: Path, hosts: frozenset[str] | None = None) -> PageSource:
    """Recorded pages, for tests, evals and the demo (INDEX decision 17): https://host/a/b is <directory>/host/a/b.html
    and the site's root is <directory>/host/index.html. With `hosts`, a host off the list is refused as live fetching
    would refuse it. A page that was not recorded is FetchFailed("not_found")."""

    def fetch(url: str) -> FetchedPage:
        host = _host_of(url)
        if host is None or (hosts is not None and not is_allowed(url, hosts)):
            raise UnsafeUrl("off_allowlist")
        segments = [s for s in urlsplit(url).path.split("/") if s]
        if any(s in (".", "..") or "\\" in s for s in segments):
            raise UnsafeUrl("invalid_url")
        path = directory / host / ("/".join(segments or ["index"]) + ".html")
        if not path.is_file():
            raise FetchFailed("not_found")
        text, title = html_to_text(path.read_text(encoding="utf-8"))
        return _page(url, text, title, REPLAY_TIME)

    return fetch


def live_source(hosts: frozenset[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> PageSource:
    """Pages from the web through http.fetch_public (private addresses refused, redirects checked there). On top of
    that: only the allowlist, a redirect that ends off the allowlist is refused, robots.txt is read once per host and
    respected for `*`, and a page over MAX_PAGE_BYTES is refused. `transport` lets a test script the answers."""
    robots: dict[str, robotparser.RobotFileParser] = {}

    def permitted(url: str, host: str) -> bool:
        if host not in robots:
            parser = robotparser.RobotFileParser()
            try:
                got = http.fetch_public(f"https://{host}/robots.txt", transport=transport)
            except (UnsafeUrl, FetchFailed):
                got = None
            if got is not None and got.status == 200 and got.body is not None:
                parser.parse(got.body.decode("utf-8", errors="replace").splitlines())
            else:
                parser.parse([])  # no readable robots.txt: nothing is disallowed
            robots[host] = parser
        return robots[host].can_fetch("*", url)

    def fetch(url: str) -> FetchedPage:
        host = _host_of(url)
        if host is None or (hosts is not None and not is_allowed(url, hosts)):
            raise UnsafeUrl("off_allowlist")
        if not permitted(url, host):
            raise UnsafeUrl("robots")
        got = http.fetch_public(url, transport=transport)
        if hosts is not None and not is_allowed(got.final_url, hosts):
            raise UnsafeUrl("redirect_off_allowlist")
        if got.status != 200:
            raise FetchFailed(f"status_{got.status}")
        if got.body is None:
            raise FetchFailed("unreadable_type")
        if len(got.body) > MAX_PAGE_BYTES:
            raise FetchFailed("too_large")
        raw = got.body.decode("utf-8", errors="replace")
        text, title = html_to_text(raw) if got.content_type == "text/html" else (raw, "")
        return _page(got.final_url, text, title, datetime.now(UTC))

    return fetch


# ---- the reader ----------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Comments, not docstrings: a docstring becomes prose in the JSON schema the provider is sent.
class RawClaim(_Strict):
    topic: Topic
    quote: str


class PageClaims(_Strict):
    claims: list[RawClaim]
    links: list[str]
    injection_suspected: bool


class PlanStep(_Strict):
    # One tool call per turn. `url` for fetch_public_page, `page_id` and `questions` for read_page, nothing for the
    # other two. The shape is checked in code so a bad turn is one refused step and not a failed run.
    tool: Literal["fetch_public_page", "read_page", "list_pages", "submit_result"]
    url: str | None = None
    page_id: str | None = None
    questions: list[str] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "PlanStep":
        wanted = {"fetch_public_page": {"url"}, "read_page": {"page_id", "questions"}}.get(self.tool, set())
        given = {name for name in ("url", "page_id", "questions") if getattr(self, name) is not None}
        if given != wanted:
            raise ValueError(f"{self.tool} takes {sorted(wanted) or 'no arguments'}")
        if self.questions is not None and not 1 <= len(self.questions) <= MAX_QUESTIONS:
            raise ValueError("a read asks one to six questions")
        return self


def _clean(text: str) -> str:
    """Page text with the marker lines of a request taken out, so a page cannot close its own data block."""
    return re.sub(r"(?i)\b(BEGIN|END) PAGE\b", "[marker removed]", text)


def read_messages(page_text: str, questions: Sequence[str]) -> list[dict[str, str]]:
    """The research.read_page request: one page between marker lines and the questions. Nothing else: no candidate,
    no campaign, no other page. Pure."""
    lines = ["Questions:"] + [f"- {' '.join(q.split())}" for q in questions[:MAX_QUESTIONS]]
    lines += ["", "BEGIN PAGE", _clean(page_text[:MAX_PAGE_CHARS]), "END PAGE"]
    return [{"role": "system", "content": READ_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


@dataclass(frozen=True)
class KeptClaim:
    topic: str
    quote: str
    url: str
    injection_suspected: bool


def keep_exact(page: FetchedPage, claims: PageClaims) -> tuple[list[KeptClaim], list[RawClaim]]:
    """(kept, dropped). A claim is kept when its quote is non-empty, not longer than MAX_QUOTE and occurs exactly in the
    page text: a paraphrase, a quote with a changed number and an empty quote are dropped. The page's injection flag
    travels with every claim from it."""
    kept: list[KeptClaim] = []
    dropped: list[RawClaim] = []
    for claim in claims.claims:
        quote = claim.quote.strip()
        if quote and len(quote) <= MAX_QUOTE and quote in page.text:
            kept.append(KeptClaim(claim.topic, quote, page.url, claims.injection_suspected))
        else:
            dropped.append(claim)
    return kept, dropped


def read_page(page: FetchedPage, questions: Sequence[str], *, principal: Any = None) -> PageClaims:
    """One research.read_page call on one page. Raises what the gateway raises (AIDisabled, SchemaInvalid ...)."""
    return cast(
        PageClaims,
        complete(
            READ_TASK, PageClaims, read_messages(page.text, questions), principal=principal, prompt_version=READ_VERSION
        ),
    )


def allowed_links(links: Iterable[str], base_url: str, hosts: frozenset[str]) -> list[str]:
    """The reader's links the planner may see: absolute https URLs on the allowlist, once each. Anything else is dropped
    (a link is how an injected page could steer the planner)."""
    out: list[str] = []
    for link in links:
        try:
            absolute = str(httpx.URL(base_url).join(link))
        except httpx.InvalidURL:
            continue
        if is_allowed(absolute, hosts) and absolute not in out:
            out.append(absolute)
    return out


# ---- the planner ----------------------------------------


def _title(page: FetchedPage, flagged: bool) -> str:
    return "" if flagged else page.title[:MAX_TITLE]


def plan_messages(
    company_name: str,
    hosts: frozenset[str],
    pages: Sequence[tuple[str, FetchedPage, bool]],
    kept: Sequence[KeptClaim],
    *,
    links: Sequence[str] = (),
    turns_left: int = MAX_TURNS,
) -> list[dict[str, str]]:
    """The research.plan request for one turn: the company name, the hosts that may be fetched, the pages fetched so far
    as (page id, page, injection flag), the claims kept so far and the allowlisted links the reader found. Never page
    text: a title is shortened and left out for a flagged page. No candidate data. Pure."""
    lines = [f"Company: {' '.join(company_name.split())}", "Hosts you may fetch from: " + ", ".join(sorted(hosts))]
    lines += ["", "Pages fetched so far (page id, url, title):"]
    lines += [
        f"- {page_id} {page.url} {_title(page, flagged)!r}" + (" [injection suspected]" if flagged else "")
        for page_id, page, flagged in pages
    ] or ["- none"]
    lines += ["", "Claims kept so far (topic, text):"]
    lines += [f"- {c.topic}: {c.quote}" for c in kept if not c.injection_suspected] or ["- none"]
    lines += ["", "Topics with no claim yet: " + (", ".join(missing_topics(kept)) or "none")]
    lines += ["Allowed links found on pages:"] + [f"- {link}" for link in links] if links else []
    lines += ["", f"Turns left: {turns_left}"]
    return [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


# ---- the dossier ----------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def build_dossier(pages: Sequence[FetchedPage], kept: Sequence[KeptClaim]) -> list[dict[str, Any]]:
    """The dossier's claims in DATA_MODEL's shape: {"text", "topic", "sources": [{"url", "quote", "retrieved_at"}],
    "injection_suspected"}. `text` is the quote itself, so no word is the model's. The same quote read from two pages is
    one claim with two sources; the first source is always set. Order is the order the claims were kept."""
    when = {page.url: page.retrieved_at for page in pages}
    merged: dict[str, dict[str, Any]] = {}
    for claim in kept:
        if claim.url not in when:
            continue  # a claim whose page is not among the pages cannot be sourced
        source = {"url": claim.url, "quote": claim.quote, "retrieved_at": when[claim.url].isoformat()}
        found = merged.get(_norm(claim.quote))
        if found is None:
            merged[_norm(claim.quote)] = {
                "text": claim.quote,
                "topic": claim.topic,
                "sources": [source],
                "injection_suspected": claim.injection_suspected,
            }
        else:
            if all(s["url"] != claim.url for s in found["sources"]):
                found["sources"].append(source)
            found["injection_suspected"] = found["injection_suspected"] or claim.injection_suspected
    return list(merged.values())


def missing_topics(claims: Iterable[Any]) -> list[str]:
    """The topics no claim covers; the dossier says "not found on the company's pages" for each (NOT_FOUND). `other` is
    not a topic anyone expects. Accepts KeptClaim objects or the dicts build_dossier returns."""
    seen = {c["topic"] if isinstance(c, dict) else c.topic for c in claims}
    return [t for t in TOPICS if t != "other" and t not in seen]


# ---- the baseline ----------------------------------------

DEFAULT_QUESTIONS = (
    "What does the company make or sell, and for whom?",
    "How large is the company and how is it funded?",
    "How does the company describe its engineering work, values or hiring process?",
)


@dataclass
class Baseline:
    pages: list[FetchedPage]
    kept: list[KeptClaim]
    dropped: int  # claims whose quote was not in the page
    failures: list[tuple[str, str]]  # (url, reason code)
    links: list[str]  # the reader's links that are on the allowlist, once each


def fixed_list(
    hosts: frozenset[str],
    posting_url: str | None,
    source: PageSource,
    *,
    questions: Sequence[str] = DEFAULT_QUESTIONS,
    principal: Any = None,
) -> Baseline:
    """The baseline (decision 16): fetch the home, about and careers pages of each allowed host and the posting, and run
    the reader once on each page, with no planner. A page that cannot be fetched is listed, never guessed."""
    urls = [f"https://{host}{path}" for host in sorted(hosts) for path in FIXED_PATHS]
    if posting_url and posting_url not in urls:
        urls.append(posting_url)
    result = Baseline([], [], 0, [], [])
    for url in urls[:MAX_TOOL_CALLS]:
        try:
            page = source(url)
        except (UnsafeUrl, FetchFailed) as failure:
            result.failures.append((url, failure.reason))
            continue
        result.pages.append(page)
        answer = read_page(page, questions, principal=principal)
        kept, dropped = keep_exact(page, answer)
        result.kept.extend(kept)
        result.dropped += len(dropped)
        result.links.extend(link for link in allowed_links(answer.links, page.url, hosts) if link not in result.links)
    return result
