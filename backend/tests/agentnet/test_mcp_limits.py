"""Rate limits of agent tokens (plan 09-04 task 2; NET-03; API 5.8; T-09-31).

The windows are tested on `Limits` with a clock the test moves, and the limits over HTTP on the real app with the same
injected clock: the 61st call in a minute and the 4th request_introduction in a day are 429 with Retry-After and a
`rate_limited` row, 20 refusals in an hour shut the token for the rest of that hour, and every limit is per token.
Everything here is invented.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.agentnet import limits
from app.agentnet.limits import Limits
from tests.agentnet.factories import call, candidate_token, rpc


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


# ---- the windows, without HTTP -------------------------------------------------------------------------------------


def test_the_61st_call_in_a_minute_waits_until_the_first_one_ages_out(clock):
    limiter, token = Limits(clock), uuid4()
    for _ in range(60):
        assert limiter.admit(token, None) is None
        clock.now += 0.5

    wait = limiter.admit(token, None)

    assert wait is not None and 1 <= wait <= 31  # the first call (30 s ago) leaves the window in 30 s
    clock.now += wait
    assert limiter.admit(token, None) is None


def test_the_1001st_call_in_a_day_is_refused_even_when_no_minute_is_full(clock):
    limiter, token = Limits(clock), uuid4()
    for _ in range(limits.CALLS_PER_DAY):
        assert limiter.admit(token, None) is None
        clock.now += 80  # under a minute's cap, and 1,000 calls span about 22 hours

    assert limiter.admit(token, None) is not None
    clock.now += limits.DAY
    assert limiter.admit(token, None) is None


def test_ask_twin_is_capped_at_30_a_day_and_request_introduction_at_3(clock):
    limiter, token = Limits(clock), uuid4()
    for _ in range(30):
        assert limiter.admit(token, "ask_twin") is None
        clock.now += 61  # the per-minute cap is not what this test is about
    assert limiter.admit(token, "ask_twin") is not None
    assert limiter.admit(token, "get_twin_answer") is None  # another tool is not affected

    for _ in range(3):
        assert limiter.admit(token, "request_introduction") is None
    assert limiter.admit(token, "request_introduction") is not None


def test_twenty_refusals_in_an_hour_shut_the_token_until_the_oldest_ages_out(clock):
    limiter, token = Limits(clock), uuid4()
    for _ in range(19):
        limiter.refused(token)
        clock.now += 10
    assert limiter.admit(token, None) is None  # 19 refusals: still open
    limiter.refused(token)

    wait = limiter.admit(token, None)

    assert wait is not None and wait > 3_000  # until the hour that holds the first refusal ends
    clock.now += wait
    assert limiter.admit(token, None) is None


def test_limits_are_per_token(clock):
    limiter, first, second = Limits(clock), uuid4(), uuid4()
    for _ in range(60):
        limiter.admit(first, None)

    assert limiter.admit(first, None) is not None
    assert limiter.admit(second, None) is None


def test_requests_without_a_token_are_counted_per_client_address_only(clock):
    limiter = Limits(clock)

    assert [limiter.unattributed("a1"), limiter.unattributed("a1"), limiter.unattributed("b2")] == [1, 2, 1]


# ---- over HTTP -----------------------------------------------------------------------------------------------------


def refused_rows(engine, token, reason):
    with engine.connect() as conn:
        return conn.execute(
            text(
                """SELECT count(*) FROM audit_log WHERE agent_token_id = :t AND event_type =
                'agentnet.agent_call_refused'
                      AND details->>'reason' = :r"""
            ),
            {"t": token, "r": reason},
        ).scalar_one()


def test_the_61st_call_in_a_minute_is_429_with_retry_after_and_a_rate_limited_row(agents, clock):
    agents.limiter.clock = clock
    secret = agents.candidate_token.secret
    for _ in range(60):
        assert rpc(agents.client, secret, "tools/list").status_code == 200

    response = rpc(agents.client, secret, "tools/list")

    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 61
    assert refused_rows(agents.engine, agents.candidate_token.id, "rate_limited") == 1
    other = agents.org_token.secret  # another token is not slowed down
    assert rpc(agents.client, other, "tools/list").status_code == 200
    clock.now += 61
    assert rpc(agents.client, secret, "tools/list").status_code == 200


def test_the_4th_request_introduction_in_a_day_is_429(agents, clock):
    agents.limiter.clock = clock
    arguments = {"role_id": str(agents.world.role.id)}
    for _ in range(3):
        assert call(agents.client, agents.org_token.secret, "request_introduction", arguments).status_code == 200
        clock.now += 61

    response = call(agents.client, agents.org_token.secret, "request_introduction", arguments)

    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert refused_rows(agents.engine, agents.org_token.id, "rate_limited") == 1


def test_after_20_refusals_in_an_hour_every_call_is_429_until_the_hour_ends(agents, clock):
    agents.limiter.clock = clock
    secret = agents.candidate_token.secret
    for _ in range(20):
        assert call(agents.client, secret, "delete_everything").status_code == 200  # -32602, one refusal each
        clock.now += 61
    assert refused_rows(agents.engine, agents.candidate_token.id, "invalid_input") == 20

    shut = rpc(agents.client, secret, "tools/list")
    still = call(agents.client, secret, "search_matches")

    assert (shut.status_code, still.status_code) == (429, 429)
    assert int(shut.headers["retry-after"]) > 0
    clock.now += int(shut.headers["retry-after"])
    assert rpc(agents.client, secret, "tools/list").status_code == 200
    with agents.engine.begin() as conn:  # a token of another person was never affected
        other = candidate_token(conn, agents.world.member.user)
    assert rpc(agents.client, other.secret, "tools/list").status_code == 200
