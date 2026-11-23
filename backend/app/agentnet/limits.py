"""Rate limits for agent tokens, in this process's memory (API 5.8, ARCHITECTURE 4.7; NET-03).

Per token: 60 calls a minute, 1,000 a day, ask_twin 30 and request_introduction 3 a day, and after 20 refused calls
within an hour every call is refused with 429 until the hour ends. The counters are sliding windows of timestamps
held in
a dict, correct for one API replica; a restart resets them (API 5.8). The clock is injected so a test can move time.

A call the limits refuse (rate_limited) is not itself counted as a refusal toward the 20, or an agent that keeps calling
would never be let back in. Requests with no valid token have no token to count against: they are only counted per
client address, and the gate logs a line with that count.
"""

import threading
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from uuid import UUID

# assumptions (API 5.8)
CALLS_PER_MINUTE = 60
CALLS_PER_DAY = 1_000
TOOL_CALLS_PER_DAY = {"ask_twin": 30, "request_introduction": 3}
REFUSALS_PER_HOUR = 20
MINUTE = 60.0
HOUR = 3_600.0
DAY = 86_400.0


class Limits:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._calls: dict[UUID, deque[float]] = defaultdict(deque)
        self._tool_calls: dict[tuple[UUID, str], deque[float]] = defaultdict(deque)
        self._refusals: dict[UUID, deque[float]] = defaultdict(deque)
        self._unattributed: Counter[str] = Counter()

    @staticmethod
    def _prune(window: deque[float], now: float, span: float) -> None:
        while window and window[0] <= now - span:
            window.popleft()

    def admit(self, token_id: UUID, tool: str | None) -> int | None:
        """Count this call and return None, or return the seconds to wait (the Retry-After) and count nothing."""
        with self._lock:
            now = self.clock()
            refusals = self._refusals[token_id]
            self._prune(refusals, now, HOUR)
            if len(refusals) >= REFUSALS_PER_HOUR:
                return _seconds(refusals[0] + HOUR - now)  # the oldest of the 20 ages out and the count drops below
            calls = self._calls[token_id]
            self._prune(calls, now, DAY)
            if len(calls) >= CALLS_PER_DAY:
                return _seconds(calls[0] + DAY - now)
            minute = [t for t in calls if t > now - MINUTE]
            if len(minute) >= CALLS_PER_MINUTE:
                return _seconds(minute[0] + MINUTE - now)
            cap = TOOL_CALLS_PER_DAY.get(tool or "")
            if cap is not None and tool is not None:
                tool_calls = self._tool_calls[(token_id, tool)]
                self._prune(tool_calls, now, DAY)
                if len(tool_calls) >= cap:
                    return _seconds(tool_calls[0] + DAY - now)
                tool_calls.append(now)
            calls.append(now)
            return None

    def refused(self, token_id: UUID) -> None:
        """Count one refused call (not rate_limited) against the token's hour."""
        with self._lock:
            self._refusals[token_id].append(self.clock())

    def unattributed(self, client: str) -> int:
        """Count a request that carried no valid token, by client address; returns how many that address has made."""
        with self._lock:
            self._unattributed[client] += 1
            return self._unattributed[client]


def _seconds(wait: float) -> int:
    return max(1, int(wait) + 1)
