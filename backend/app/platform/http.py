"""Outbound fetching of addresses a third party chose (ARCHITECTURE 7.6; threats T-02-23 and T-02-25).

The board fetchers contact three fixed hosts and need none of this. Everything else (an apply link from a feed) goes
through fetch_public(url), which reads one public web page:

- HTTP or HTTPS only, and the URL must name a host;
- refused if any address the host resolves to is not a public one (private, loopback, link-local, the cloud metadata
  address, unspecified, reserved), and the same check is made again for every redirect, at most 5 of them;
- at most 5 MB and 15 s, and only HTML, plain text and JSON are read; any other type gives the status alone. The 15 s is
  a budget for the whole call: each wait is limited to what is left of it, the connect time is shared among the host's
  addresses, and a body that drips in is cut when the budget is spent. A read that began just before the end can still
  wait out its own limit, so a hostile server can hold a call for up to twice the budget and no longer.

It sends no credentials and keeps no state between calls. A refusal is UnsafeUrl and any other failure is FetchFailed,
each with a short reason code. The log gets the reason code and never the URL, which a feed supplied.
"""

import ipaddress
import socket
import time
from dataclasses import dataclass

import httpx

from . import log

USER_AGENT = "open-career-agent-indexer/0.1"
MAX_BYTES = 5 * 1024 * 1024  # ARCHITECTURE 7.6, an assumption there
TIMEOUT_S = 15  # the budget for the whole call (ARCHITECTURE 7.6, an assumption there)
MAX_REDIRECTS = 5
ALLOWED_TYPES = frozenset({"text/html", "text/plain", "application/json"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafeUrl(Exception):
    """The URL, or a redirect it led to, may not be fetched. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FetchFailed(Exception):
    """The fetch was allowed but did not finish. `reason` is timeout, network, too_large or too_many_redirects."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublicResponse:
    status: int
    final_url: str  # after redirects
    content_type: str | None  # the media type alone, lower case; None when the answer names none
    body: bytes | None  # None when the type is not one we read


def resolve(host: str) -> list[str]:
    """Every address `host` resolves to. This is the only place the module asks DNS, so tests replace it."""
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback whatever the Python version says about it
    return ip.is_global


def _check_public(url: httpx.URL) -> int:
    """Refuse a URL that may not be fetched. Returns how many addresses its host resolves to."""
    if url.scheme not in ("http", "https"):
        raise UnsafeUrl("scheme")
    host = url.raw_host.decode("ascii")  # the very name httpx will connect to
    if not host:
        raise UnsafeUrl("no_host")
    try:
        addresses = resolve(host)
    except UnicodeError as error:  # a label that is empty or over 63 characters never reaches DNS
        raise UnsafeUrl("invalid_url") from error
    except socket.gaierror as error:
        raise FetchFailed("network") from error
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeUrl("non_public_address")
    return len(addresses)


def _url(text: str, base: httpx.URL | None = None) -> httpx.URL:
    try:
        return base.join(text) if base is not None else httpx.URL(text)
    except httpx.InvalidURL as error:
        raise UnsafeUrl("invalid_url") from error


def _read(response: httpx.Response, url: httpx.URL, deadline: float) -> PublicResponse:
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower() or None
    # A compressed body can inflate far past the cap in one step, so only a body sent as it is gets read (which also
    # means iter_bytes below decodes nothing). The request asks for no compression, and a server that sends one anyway
    # costs us the body, never the status.
    plain = response.headers.get("content-encoding", "identity").lower() == "identity"
    body = None
    if media_type in ALLOWED_TYPES and plain:
        pieces = []
        size = 0
        for piece in response.iter_bytes():  # one socket read at a time, up to 64 KB
            size += len(piece)
            if size > MAX_BYTES:
                raise FetchFailed("too_large")
            if time.monotonic() > deadline:  # a body that drips in never trips a single read's timeout
                raise FetchFailed("timeout")
            pieces.append(piece)
        body = b"".join(pieces)
    return PublicResponse(response.status_code, str(url), media_type, body)


def _fetch(url: str, transport: httpx.BaseTransport | None) -> PublicResponse:
    current = _url(url)
    deadline = time.monotonic() + TIMEOUT_S
    # trust_env=False: a proxy named in the environment would resolve the host itself, and the check below would say
    # nothing about where the request really goes.
    with httpx.Client(
        transport=transport,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            addresses = _check_public(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchFailed("timeout")
            # httpx tries each address the name resolved to in turn, each with the connect timeout, so the connect
            # budget is split among them: four dead addresses must not hold the call for four times the budget.
            timeout = httpx.Timeout(remaining, connect=remaining / addresses)
            # ponytail: the host is resolved here and again when httpx connects, so a DNS answer that changes in
            # between (rebinding) is not caught. Closing it means connecting to the address checked here, with the
            # Host header and TLS name kept. Add it when fetch_public carries more than reads of public pages.
            try:
                with client.stream("GET", current, timeout=timeout) as response:
                    if response.status_code not in REDIRECT_STATUSES or "location" not in response.headers:
                        return _read(response, current, deadline)
                    location = response.headers["location"]
            except httpx.TimeoutException as error:
                raise FetchFailed("timeout") from error
            except httpx.HTTPError as error:
                raise FetchFailed("network") from error
            current = _url(location, current)
    raise FetchFailed("too_many_redirects")


def fetch_public(url: str, *, transport: httpx.BaseTransport | None = None) -> PublicResponse:
    """GET `url` under the rules in the module docstring. `transport` exists so tests can pass httpx.MockTransport."""
    try:
        return _fetch(url, transport)
    except (UnsafeUrl, FetchFailed) as error:
        outcome = "refused" if isinstance(error, UnsafeUrl) else "failed"
        log.event("http.fetch_public", outcome=outcome, error_code=error.reason)
        raise
