"""fetch_public, the only outbound fetch for URLs a third party chose (ARCHITECTURE 7.6; threats T-02-23, T-02-25).

No test touches the network. The resolver is replaced by a table of invented answers (an IP literal resolves to itself,
as it does for real), and every request goes to an httpx.MockTransport that records what it was asked for.
"""

import ipaddress
import json
import logging
import socket
import time
import types

import httpx
import pytest

from app.platform import http

PUBLIC = "93.184.216.34"  # a public address as far as the check can tell; nothing is ever sent to it
NAMES = {
    "example.test": [PUBLIC],
    "careers.example.test": [PUBLIC],
    "mixed.test": [PUBLIC, "10.0.0.5"],  # one private answer among public ones is enough to refuse
    "internal.test": ["10.0.0.5"],
    "localhost": ["127.0.0.1", "::1"],
    "2130706433": ["127.0.0.1"],  # what the system resolver makes of the decimal spelling of 127.0.0.1
    "0x7f.1": ["127.0.0.1"],
}
JOB = "https://example.test/jobs/1"
REAL_RESOLVE = http.resolve
PIECE = 64 * 1024  # the most a real socket read hands over at once


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    def resolve(host):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host not in NAMES:
                raise socket.gaierror("no such host") from None
            return NAMES[host]
        return [host]

    monkeypatch.setattr(http, "resolve", resolve)


def named(request):
    """The URL the caller asked for. The request itself goes to a checked address, with the name in the Host header."""
    return f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode('ascii')}"


def served(handler):
    """(transport, requested): `handler` answers every request, and every URL it was asked for is listed, by name."""
    requested = []

    def record(request):
        requested.append(named(request))
        return handler(request)

    return httpx.MockTransport(record), requested


def page(body=b"<html><body>Senior engineer</body></html>", content_type="text/html; charset=utf-8", status=200):
    return httpx.Response(status, headers={"content-type": content_type}, content=body)


def redirect(location, status=302):
    return httpx.Response(status, headers={"location": location})


def refused(url):
    """What fetch_public says when it refuses `url`, after checking that nothing was requested."""
    transport, requested = served(lambda request: pytest.fail("a request was sent"))
    with pytest.raises(http.UnsafeUrl) as error:
        http.fetch_public(url, transport=transport)
    assert requested == []
    return error.value.reason


class Unreadable(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("the body was read")


class Counted(httpx.SyncByteStream):
    """`total` bytes in pieces of a socket read's size, remembering how many it has handed out."""

    def __init__(self, total):
        self.total, self.sent = total, 0

    def __iter__(self):
        while self.sent < self.total:
            piece = min(PIECE, self.total - self.sent)
            self.sent += piece
            yield b"x" * piece


# --- what may be requested at all ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/file",
        "file:///etc/passwd",
        "data:text/html,hello",
        "javascript:alert(1)",
        "example.test/jobs/1",  # no scheme
        "//example.test/jobs/1",
        "http:///jobs/1",  # no host
        "",
    ],
)
def test_a_url_that_is_not_http_or_https_with_a_host_is_refused_before_any_request(url):
    assert refused(url) in {"scheme", "no_host"}


def test_a_url_that_cannot_be_read_is_refused():
    assert refused("http://[::1") == "invalid_url"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3:8080/admin",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # the cloud metadata address
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",  # an IPv4 loopback address written as IPv6
        "http://[::ffff:169.254.169.254]/",
        "http://[64:ff9b::7f00:1]/",  # NAT64 wrapping 127.0.0.1
        "http://[64:ff9b::a00:1]/",  # NAT64 wrapping 10.0.0.1: a gateway would carry it to the private network
        "http://[2002:7f00:1::]/",  # 6to4 wrapping 127.0.0.1
        "http://[2002:a9fe:a9fe::]/",  # 6to4 wrapping the cloud metadata address
        "http://[::127.0.0.1]/",  # the deprecated IPv4-compatible form
        "http://[::10.0.0.1]/",
        "http://[fec0::1]/",  # site-local, deprecated but still private
        "http://[ff02::1]/",  # multicast
        "http://[ff0e::1]/",
        "http://224.0.0.1/",  # IPv4 multicast
        "http://[::ffff:224.0.0.1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://localhost/",
        "http://internal.test/",
        "http://mixed.test/",
    ],
)
def test_an_address_that_is_not_public_is_refused_before_any_request(url):
    assert refused(url) == "non_public_address"


def test_a_resolver_that_answers_with_no_address_at_all_is_refused_not_trusted(monkeypatch):
    monkeypatch.setitem(NAMES, "empty.test", [])
    assert refused("http://empty.test/jobs/1") == "non_public_address"


def test_a_name_that_does_not_resolve_is_a_network_failure_not_a_refusal():
    transport, requested = served(lambda request: pytest.fail("a request was sent"))
    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public("https://nowhere.test/jobs/1", transport=transport)
    assert error.value.reason == "network" and requested == []


@pytest.mark.parametrize("host", ["a" * 70 + ".test", "a..test"])
def test_a_host_name_that_cannot_be_encoded_is_refused_not_a_crash(monkeypatch, host):
    monkeypatch.setattr(http, "resolve", REAL_RESOLVE)  # the system encoder rejects these before any DNS query
    assert refused(f"http://{host}/jobs/1") == "invalid_url"


def test_the_system_resolver_is_asked_for_the_host_and_returns_addresses_only():
    assert REAL_RESOLVE("127.0.0.1") == ["127.0.0.1"]  # an IP literal needs no network


@pytest.mark.parametrize(
    "host",
    [
        "[64:ff9b::5db8:d822]",  # NAT64 wrapping a public IPv4 address, which a DNS64 resolver hands out for real hosts
        "[2002:5db8:d822::]",  # 6to4 wrapping a public IPv4 address
        "[::ffff:93.184.216.34]",
        "[2606:4700:4700::1111]",  # a plain public IPv6 address
    ],
)
def test_a_public_address_inside_a_transition_form_is_still_fetched(host):
    transport, requested = served(lambda request: page())
    assert http.fetch_public(f"http://{host}/jobs/1", transport=transport).status == 200
    assert len(requested) == 1


# --- where the request goes: the address that was checked ---------------------------------------------------------


def test_the_request_goes_to_the_address_that_passed_the_check_with_the_name_kept_for_host_and_tls():
    seen = {}

    def handler(request):
        seen.update(address=request.url.host, host=request.headers["host"], tls_name=request.extensions["sni_hostname"])
        return page()

    http.fetch_public(JOB, transport=served(handler)[0])

    assert seen == {"address": PUBLIC, "host": "example.test", "tls_name": "example.test"}


def test_a_port_stays_on_the_connection_and_in_the_host_header():
    seen = {}

    def handler(request):
        seen.update(port=request.url.port, host=request.headers["host"])
        return page()

    http.fetch_public("https://example.test:8443/jobs/1", transport=served(handler)[0])

    assert seen == {"port": 8443, "host": "example.test:8443"}


def test_a_name_whose_dns_answer_changes_after_the_check_is_never_connected_to_at_the_new_answer(monkeypatch):
    """DNS rebinding: a name answers with a public address when it is checked and with loopback when it is asked again.
    The request must go to the address that passed, so the transport only ever sees the public one."""
    asked, connected = [], []

    def rebinding(host):
        asked.append(host)
        return [PUBLIC] if asked.count(host) == 1 else ["127.0.0.1"]

    monkeypatch.setattr(http, "resolve", rebinding)
    answers = {JOB: redirect("https://careers.example.test/jobs/1"), "https://careers.example.test/jobs/1": page()}

    def handler(request):
        connected.append(request.url.host)
        return answers[named(request)]

    assert http.fetch_public(JOB, transport=served(handler)[0]).status == 200
    assert connected == [PUBLIC, PUBLIC]  # one address per hop, never loopback
    assert asked == ["example.test", "careers.example.test"]  # each name is asked once, and not again to connect


def test_every_request_closes_its_connection_so_a_hop_to_another_name_never_reuses_it():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return page()

    http.fetch_public(JOB, transport=served(handler)[0])

    assert seen["connection"] == "close"


def test_the_next_checked_address_is_tried_when_one_takes_no_connection(monkeypatch):
    # A host with an IPv6 and an IPv4 address, fetched from a machine with no IPv6 route: the system's own connect
    # would move on to the other address, and so does this.
    monkeypatch.setitem(NAMES, "dual.test", ["2606:4700:4700::1111", PUBLIC])
    tried = []

    def handler(request):
        tried.append(request.url.host)
        if request.url.host == "2606:4700:4700::1111":
            raise httpx.ConnectError("network unreachable")
        return page()

    assert http.fetch_public("https://dual.test/jobs/1", transport=served(handler)[0]).status == 200
    assert tried == ["2606:4700:4700::1111", PUBLIC]


def test_a_failure_after_the_connection_was_made_does_not_move_on_to_the_next_address(monkeypatch):
    monkeypatch.setitem(NAMES, "dual.test", [PUBLIC, "93.184.216.35"])
    tried = []

    def handler(request):
        tried.append(request.url.host)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public("https://dual.test/jobs/1", transport=served(handler)[0])

    assert error.value.reason == "timeout" and tried == [PUBLIC]


@pytest.mark.parametrize(
    ("failure", "reason"), [(httpx.ConnectTimeout("dead"), "timeout"), (httpx.ConnectError("refused"), "network")]
)
def test_when_no_address_takes_the_connection_the_fetch_fails_with_the_last_reason(monkeypatch, failure, reason):
    monkeypatch.setitem(NAMES, "dead.test", [PUBLIC, "93.184.216.35"])
    tried = []

    def handler(request):
        tried.append(request.url.host)
        raise failure

    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public("https://dead.test/jobs/1", transport=served(handler)[0])

    assert error.value.reason == reason and tried == [PUBLIC, "93.184.216.35"]


# --- redirects ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/healthz",
        "http://internal.test/admin",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://example.test/file",
    ],
)
def test_a_redirect_to_somewhere_not_allowed_is_refused_and_never_requested(location):
    transport, requested = served(lambda request: redirect(location))
    with pytest.raises(http.UnsafeUrl):
        http.fetch_public(JOB, transport=transport)
    assert requested == [JOB]


def test_redirects_are_followed_with_relative_ones_resolved_and_the_final_url_reported():
    answers = {
        "https://example.test/old": redirect("/jobs/1", 301),
        JOB: redirect("https://careers.example.test/jobs/1?ref=feed", 307),
        "https://careers.example.test/jobs/1?ref=feed": page(),
    }
    transport, requested = served(lambda request: answers[named(request)])

    result = http.fetch_public("https://example.test/old", transport=transport)

    assert result.final_url == "https://careers.example.test/jobs/1?ref=feed" and result.status == 200
    assert requested == ["https://example.test/old", JOB, "https://careers.example.test/jobs/1?ref=feed"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_status_is_followed(status):
    answers = {JOB: redirect("/jobs/2", status), "https://example.test/jobs/2": page()}
    transport, _ = served(lambda request: answers[named(request)])
    assert http.fetch_public(JOB, transport=transport).final_url == "https://example.test/jobs/2"


def chain(length):
    """A handler that redirects `length` times and then answers."""

    def handler(request):
        step = int(request.url.path.rsplit("/", 1)[1])
        return redirect(f"/hop/{step + 1}") if step < length else page()

    return handler


def test_five_redirects_are_followed():
    transport, requested = served(chain(5))
    assert http.fetch_public("https://example.test/hop/0", transport=transport).status == 200
    assert len(requested) == 6


def test_a_sixth_redirect_is_refused_and_not_followed():
    transport, requested = served(chain(6))
    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public("https://example.test/hop/0", transport=transport)
    assert error.value.reason == "too_many_redirects"
    assert requested[-1] == "https://example.test/hop/5" and len(requested) == 6


def test_a_redirect_with_no_location_is_just_an_answer():
    transport, _ = served(lambda request: httpx.Response(302))
    assert http.fetch_public(JOB, transport=transport).status == 302


# --- what is read -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "media_type"),
    [
        ("text/html; charset=utf-8", "text/html"),
        ("TEXT/HTML", "text/html"),
        ("application/json", "application/json"),
        ("text/plain", "text/plain"),
    ],
)
def test_html_plain_text_and_json_are_read(content_type, media_type):
    transport, _ = served(lambda request: page(b"hello", content_type))
    assert http.fetch_public(JOB, transport=transport) == http.PublicResponse(200, JOB, media_type, b"hello")


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png", "application/octet-stream", "text/xml", None])
def test_any_other_type_returns_the_status_with_no_body_and_the_body_unread(content_type):
    headers = {} if content_type is None else {"content-type": content_type}
    transport, _ = served(lambda request: httpx.Response(200, headers=headers, stream=Unreadable()))
    assert http.fetch_public(JOB, transport=transport) == http.PublicResponse(200, JOB, content_type, None)


def test_a_compressed_body_is_not_read_because_it_can_inflate_past_the_cap():
    headers = {"content-type": "text/html", "content-encoding": "gzip"}
    transport, _ = served(lambda request: httpx.Response(200, headers=headers, stream=Unreadable()))
    assert http.fetch_public(JOB, transport=transport).body is None


@pytest.mark.parametrize("status", [404, 410, 403, 429, 500, 503])
def test_an_error_status_is_an_answer_not_an_exception(status):
    transport, _ = served(lambda request: page(b"gone", status=status))
    assert http.fetch_public(JOB, transport=transport).status == status


def test_the_request_names_the_indexer_and_asks_for_no_compression():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return page()

    http.fetch_public(JOB, transport=served(handler)[0])
    assert seen["user-agent"] == http.USER_AGENT and seen["accept-encoding"] == "identity"


# --- limits -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("size", "allowed"), [(http.MAX_BYTES, True), (http.MAX_BYTES + 1, False)])
def test_the_size_cap_is_5_mb_exactly(size, allowed):
    transport, _ = served(lambda request: page(b"x" * size))
    if allowed:
        assert len(http.fetch_public(JOB, transport=transport).body) == size
    else:
        with pytest.raises(http.FetchFailed) as error:
            http.fetch_public(JOB, transport=transport)
        assert error.value.reason == "too_large"


def test_a_huge_body_stops_being_read_one_piece_after_the_cap():
    body = Counted(4 * http.MAX_BYTES)
    transport, _ = served(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, stream=body))

    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public(JOB, transport=transport)

    assert error.value.reason == "too_large"
    assert body.sent <= http.MAX_BYTES + PIECE  # not 20 MB


def test_connect_time_is_shared_among_the_addresses_so_a_name_with_many_dead_ones_cannot_hold_the_call(monkeypatch):
    # Each resolved address is tried in turn, and a dead one costs its whole connect timeout: four dead ones must not
    # cost 4 x 15 s. The clock below moves on by exactly the time each dead address was given.
    monkeypatch.setitem(NAMES, "many.test", [f"93.184.216.{n}" for n in range(30, 34)])
    now = [0.0]
    monkeypatch.setattr(http, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    connect_timeouts, longest_wait = [], []

    def handler(request):
        timeout = request.extensions["timeout"]
        connect_timeouts.append(timeout["connect"])
        longest_wait.append(max(timeout.values()))
        now[0] += timeout["connect"]
        raise httpx.ConnectTimeout("dead")

    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public("https://many.test/jobs/1", transport=served(handler)[0])

    assert error.value.reason == "timeout" and len(connect_timeouts) == 4
    assert connect_timeouts[0] <= http.TIMEOUT_S / 4  # the first try is a quarter of the budget
    assert now[0] <= http.TIMEOUT_S  # all four together are the budget and no more
    assert max(longest_wait) <= http.TIMEOUT_S  # no single wait is longer than the whole budget


def test_a_chain_of_redirects_that_has_spent_the_budget_is_not_followed_further(monkeypatch):
    monkeypatch.setattr(http, "TIMEOUT_S", 0.1)

    def handler(request):
        time.sleep(0.15)  # the first hop alone uses up the whole budget
        return redirect("/jobs/2")

    transport, requested = served(handler)
    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public(JOB, transport=transport)

    assert error.value.reason == "timeout" and requested == [JOB]


def test_a_body_that_drips_in_runs_into_the_time_limit(monkeypatch):
    monkeypatch.setattr(http, "TIMEOUT_S", 0.2)

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):  # five seconds of it if the limit did not fire
                time.sleep(0.05)
                yield b"x"

    transport, _ = served(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, stream=Drip()))
    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public(JOB, transport=transport)
    assert error.value.reason == "timeout"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (httpx.PoolTimeout("busy"), "timeout"),
        (httpx.ConnectError("refused"), "network"),
        (httpx.ReadError("reset"), "network"),
        (httpx.RemoteProtocolError("garbled"), "network"),
    ],
)
def test_a_transport_failure_becomes_fetch_failed_with_a_reason(failure, reason):
    def handler(request):
        raise failure

    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public(JOB, transport=served(handler)[0])
    assert error.value.reason == reason


def test_a_connection_that_breaks_in_the_middle_of_the_body_is_a_network_failure():
    class Breaks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"<html>"
            raise httpx.ReadError("reset")

    transport, _ = served(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, stream=Breaks()))
    with pytest.raises(http.FetchFailed) as error:
        http.fetch_public(JOB, transport=transport)
    assert error.value.reason == "network"


# --- logging ------------------------------------------------------------------------------------------------------


def test_a_refusal_is_logged_by_its_reason_code_and_never_by_url(caplog):
    caplog.set_level(logging.INFO, logger="app.event")

    refused("http://127.0.0.1/admin?token=canary-token")

    lines = [json.loads(record.getMessage()) for record in caplog.records if record.name == "app.event"]
    assert [(line["event"], line["outcome"], line["error_code"]) for line in lines] == [
        ("http.fetch_public", "refused", "non_public_address")
    ]
    assert "canary-token" not in caplog.text and "127.0.0.1" not in caplog.text


def test_a_failure_is_logged_by_its_reason_code_too(caplog):
    caplog.set_level(logging.INFO, logger="app.event")

    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(http.FetchFailed):
        http.fetch_public(JOB, transport=served(handler)[0])

    (line,) = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.event"]
    assert (line["outcome"], line["error_code"]) == ("failed", "network")
    assert "example.test" not in caplog.text
