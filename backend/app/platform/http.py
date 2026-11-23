"""Outbound fetching of addresses a third party chose (ARCHITECTURE 7.6; threats T-02-23 and T-02-25).

The board fetchers contact three fixed hosts and need none of this. Everything else (an apply link from a feed) goes
through fetch_public(url), which reads one public web page:

- HTTP or HTTPS only, and the URL must name a host;
- refused if any address the host resolves to is not a public one (private, loopback, link-local, the cloud metadata
  address, unspecified, reserved, multicast, site-local; an IPv6 address that wraps an IPv4 one, as ::ffff:, 6to4, NAT64
  and ::/96 do, is judged by the IPv4 address inside it), and the same check is made again for every redirect, at most
  5 of them;
- the request is sent to an address that passed the check, never to the name: DNS is asked once per hop, so an answer
  that changes between the check and the connection (rebinding) is never used. The name stays in the Host header and in
  the TLS handshake, so the certificate is still checked against it;
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
from contextlib import AbstractContextManager
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


# IPv6 forms that carry an IPv4 address in their last 32 bits: NAT64's well-known prefix and the deprecated
# IPv4-compatible form. 6to4 (2002::/16) carries it in bits 16 to 47, which `sixtofour` reads. Python 3.11's is_global
# says yes to all of these, and to site-local fec0::/10 and to multicast, so they are judged here.
_IPV4_IN_LAST_32 = (ipaddress.IPv6Network("64:ff9b::/96"), ipaddress.IPv6Network("::/96"))
_SITE_LOCAL = ipaddress.IPv6Network("fec0::/10")


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _SITE_LOCAL:
            return False
        # An address that wraps an IPv4 one is as public as the one inside it: ::ffff:127.0.0.1 is loopback whatever the
        # Python version says about it, and 64:ff9b::a00:1 reaches 10.0.0.1 through a NAT64 gateway.
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif any(ip in network for network in _IPV4_IN_LAST_32):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def _check_public(url: httpx.URL) -> list[str]:
    """Refuse a URL that may not be fetched. Returns the addresses its host resolves to, every one of them public."""
    if url.scheme not in ("http", "https"):
        raise UnsafeUrl("scheme")
    host = url.raw_host.decode("ascii")  # the very name httpx would connect to
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
    return addresses


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


def _stream_to(
    client: httpx.Client, url: httpx.URL, address: str, timeout: httpx.Timeout
) -> AbstractContextManager[httpx.Response]:
    """GET `url` from `address`, one of the addresses that passed the check, so the name is not resolved a second time.
    The URL's own name stays in the Host header (with its port, as httpx would have sent it) and is the TLS name, so
    the certificate is still checked against it."""
    return client.stream(
        "GET",
        url.copy_with(host=address),
        headers={"Host": url.netloc.decode("ascii")},
        extensions={"sni_hostname": url.raw_host.decode("ascii")},
        timeout=timeout,
    )


def _hop(client: httpx.Client, url: httpx.URL, addresses: list[str], deadline: float) -> PublicResponse | str:
    """One request of the chain. The checked addresses are tried in turn, as the system does for a name, and the next
    one only when this one would not take the connection. Returns the answer, or the Location it redirects to."""
    failure: Exception | None = None
    for position, address in enumerate(addresses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchFailed("timeout")
        # Each try may spend an equal share of what is left on connecting: four dead addresses must not hold the call
        # for four times the budget.
        timeout = httpx.Timeout(remaining, connect=remaining / (len(addresses) - position))
        try:
            with _stream_to(client, url, address, timeout) as response:
                if response.status_code not in REDIRECT_STATUSES or "location" not in response.headers:
                    return _read(response, url, deadline)
                return response.headers["location"]
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            failure = error
        except httpx.TimeoutException as error:
            raise FetchFailed("timeout") from error
        except httpx.HTTPError as error:
            raise FetchFailed("network") from error
    raise FetchFailed("timeout" if isinstance(failure, httpx.ConnectTimeout) else "network") from failure


def _fetch(url: str, transport: httpx.BaseTransport | None) -> PublicResponse:
    current = _url(url)
    deadline = time.monotonic() + TIMEOUT_S
    # trust_env=False: a proxy named in the environment would resolve the host itself, and the check below would say
    # nothing about where the request really goes. Connection: close gives every request a connection of its own, so
    # the pool never carries a hop to another name over a connection whose certificate was checked for this one.
    with httpx.Client(
        transport=transport,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"},
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            answer = _hop(client, current, _check_public(current), deadline)
            if isinstance(answer, PublicResponse):
                return answer
            current = _url(answer, current)
    raise FetchFailed("too_many_redirects")


def fetch_public(url: str, *, transport: httpx.BaseTransport | None = None) -> PublicResponse:
    """GET `url` under the rules in the module docstring. `transport` exists so tests can pass httpx.MockTransport."""
    try:
        return _fetch(url, transport)
    except (UnsafeUrl, FetchFailed) as error:
        outcome = "refused" if isinstance(error, UnsafeUrl) else "failed"
        log.event("http.fetch_public", outcome=outcome, error_code=error.reason)
        raise
