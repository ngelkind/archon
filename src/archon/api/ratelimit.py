"""Rate limiting for the publicly-reachable product API.

The single-user deployment never needed this: the API was bound to WireGuard, so
the tunnel *was* the rate limiter — an attacker had to already be a peer. A
hosted multi-tenant product has no such gate, which makes credential guessing on
sign-in and pair-code brute force the obvious first attacks.

Three budgets, all sliding-window over 60s and all in-process:

* **auth endpoints, per IP** — the tightest. ``/auth/signup``, ``/auth/login``,
  ``/auth/refresh`` and ``/pair`` are where secrets are guessed; a 6-digit pair
  code is only 10^6 wide, so an unlimited caller finds one in minutes.
* **all endpoints, per IP** — blunt protection against a single noisy client.
* **all endpoints, per tenant** — one account cannot exhaust the process for
  everyone else, whatever IPs it comes from.

In-process state is the right scope *today* (one process, one box). It stops
being sufficient the moment the API runs multiple replicas — noted in the report
as a Postgres/Redis trigger rather than pretended away here.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

_WINDOW_S = 60.0

#: Paths where credentials are presented; guessed cheaply if left unlimited.
AUTH_PATHS = ("/auth/signup", "/auth/login", "/auth/refresh", "/pair")


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


class RateLimiter:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.by_ip = SlidingWindow()
        self.by_tenant = SlidingWindow()
        self.by_auth_ip = SlidingWindow()

    @staticmethod
    def client_ip(request: Request) -> str:
        client = request.client
        return client.host if client else "unknown"

    def check(self, request: Request, tenant_id: int | None = None) -> tuple[bool, int]:
        """(allowed, retry_after_seconds)."""
        s = self.settings
        ip = self.client_ip(request)
        path = request.url.path

        if any(path.startswith(p) for p in AUTH_PATHS):
            if not self.by_auth_ip.hit(ip, s.rate_limit_auth_per_ip_per_min):
                return False, self.by_auth_ip.retry_after(ip)
        if not self.by_ip.hit(ip, s.rate_limit_per_ip_per_min):
            return False, self.by_ip.retry_after(ip)
        if tenant_id is not None and not self.by_tenant.hit(
            str(tenant_id), s.rate_limit_per_tenant_per_min
        ):
            return False, self.by_tenant.retry_after(str(tenant_id))
        return True, 0


def install(app, settings) -> None:
    """Attach the limiter, but only in multi-tenant mode.

    The single-user tunnel deployment keeps its current behaviour exactly; there
    is no point rate-limiting a WireGuard peer that is already the owner.
    """
    if not settings.multitenant_enabled:
        return
    limiter = RateLimiter(settings)
    app.state.rate_limiter = limiter

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        # The tenant is not known before auth runs, so the pre-flight check is
        # per-IP; the per-tenant budget is charged once the request resolves.
        allowed, retry_after = limiter.check(request)
        if not allowed:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        response = await call_next(request)
        tenant_id = getattr(request.state, "tenant_id", None)
        if tenant_id is not None:
            ok, retry = limiter.check(request, tenant_id=tenant_id)
            if not ok:
                return JSONResponse(
                    {"detail": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
        return response
