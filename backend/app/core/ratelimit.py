"""Simple in-memory sliding-window rate limiter (single process, good enough for one instance)."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float, clock=time.monotonic):  # type: ignore[no-untyped-def]
        self.limit = limit
        self.window = window_s
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def hit(self, key: str) -> bool:
        """Record an attempt. Returns False if the key is over its limit."""
        now = self.clock()
        q = self._prune(key, now)
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 10_000:  # bound memory under a flood of distinct keys
            for k in [k for k, v in self._hits.items() if not v][:5000]:
                del self._hits[k]
        return True

    def retry_after(self, key: str) -> int:
        q = self._prune(key, self.clock())
        return int(q[0] + self.window - self.clock()) + 1 if q else 0

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
