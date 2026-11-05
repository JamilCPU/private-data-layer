"""Request limits and the sliding-window rate limiter."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

MAX_BODY_BYTES = 65536
MAX_BATCH = 100
RATE_LIMIT = 60
RATE_WINDOW_S = 60.0
_PRUNE_AT = 4096


def utcnow() -> datetime:
    return datetime.now(UTC)


class RateLimiter:
    """At most ``limit`` accepted hits per sliding ``window_s`` for each key."""

    def __init__(
        self,
        limit: int = RATE_LIMIT,
        window_s: float = RATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def _expire(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self._window_s:
            hits.popleft()

    def _prune(self, now: float) -> None:
        for key in list(self._hits):
            self._expire(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]

    def allow(self, key: str) -> bool:
        now = self._clock()
        if len(self._hits) >= _PRUNE_AT:
            self._prune(now)
        hits = self._hits.setdefault(key, deque())
        self._expire(hits, now)
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until the oldest hit leaves the window (at least 1)."""
        hits = self._hits.get(key)
        if not hits:
            return 1
        wait = hits[0] + self._window_s - self._clock()
        return max(1, math.ceil(wait))
