"""Sliding-window counter, shared by the API rate limiter and outbound pacing.

Lives here rather than in ``api/`` because the outbound pacer needs it too, and
``api/ratelimit.py`` imports FastAPI — pulling a web framework into the send
path to reuse forty lines of stdlib would be the wrong trade. Pure stdlib, no
project imports, so anything may depend on it.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

_WINDOW_S = 60.0


class SlidingWindow:
    """Per-key hit timestamps within the trailing window."""

    def __init__(self, window_s: float = _WINDOW_S) -> None:
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, now: float | None = None) -> bool:
        """Record a hit; return True when it is allowed. ``limit`` 0 disables."""
        if limit <= 0:
            return True
        now = now if now is not None else time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def would_allow(self, key: str, limit: int, now: float | None = None) -> bool:
        """Whether :meth:`hit` would succeed, WITHOUT recording anything.

        Needed because the pacer checks several budgets before sending: charging
        a per-tenant hit and then refusing on the global budget would bill a
        tenant for a message that never went out, and repeated refusals would
        eventually lock them out of their own quota.
        """
        if limit <= 0:
            return True
        now = now if now is not None else time.monotonic()
        bucket = self._hits.get(key)
        if not bucket:
            return True
        cutoff = now - self.window_s
        live = sum(1 for t in bucket if t >= cutoff)
        return live < limit

    def retry_after(self, key: str, now: float | None = None) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            return 1
        now = now if now is not None else time.monotonic()
        return max(1, int(self.window_s - (now - bucket[0])) + 1)

    def prune(self, now: float | None = None) -> None:
        """Drop empty buckets so keys from one-off IPs are not kept forever."""
        now = now if now is not None else time.monotonic()
        cutoff = now - self.window_s
        for key in [k for k, b in self._hits.items() if not b or b[-1] < cutoff]:
            self._hits.pop(key, None)
