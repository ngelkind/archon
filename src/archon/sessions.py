"""Per-tenant integration session registry.

The single-user bot holds exactly one WhatsApp session, one Telethon session and
one Google credential, parked in ``rt.clients``. That is the deepest
single-tenant assumption in the codebase: those are *per-process* objects, and a
multi-user product needs one **per tenant**, isolated, created on demand and
released when idle.

This module owns that lifecycle and nothing else. It deliberately does NOT know
how to build a WhatsApp or Google client — factories are registered by the
integration modules (next task), exactly like ``confirm.register_executor``.
That keeps this file import-cycle-free and lets the interface land before the
per-integration wiring exists.

Lifecycle
---------
``get()``   lazily builds a session via the registered factory and caches it.
``touch``   every access refreshes ``last_used_at``.
``evict``   closes one tenant's session(s) — used on logout, unlink, revoke.
``sweep``   closes sessions idle past ``max_idle_s``; a capacity cap evicts the
            least-recently-used tenant so one process cannot hold unbounded
            live sessions.

Isolation is the point: a session is keyed by ``(tenant_id, kind)`` and is only
ever handed to a caller that already proved it holds that tenant's context.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Integration kinds a tenant can hold a live session for.
SessionKind = str
KNOWN_KINDS: frozenset[str] = frozenset({"google", "telegram", "whatsapp"})

#: factory(rt, tenant_id) -> client object (may be async)
SessionFactory = Callable[[Any, int], Any | Awaitable[Any]]

_DEFAULT_MAX_IDLE_S = 30 * 60
_DEFAULT_MAX_SESSIONS = 200


@dataclass
class Session:
    tenant_id: int
    kind: SessionKind
    client: Any
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used_at = time.time()


class SessionRegistry:
    """Lazily-built, per-tenant integration clients with idle eviction."""

    def __init__(self, *, max_idle_s: int = _DEFAULT_MAX_IDLE_S,
                 max_sessions: int = _DEFAULT_MAX_SESSIONS) -> None:
        self._factories: dict[SessionKind, SessionFactory] = {}
        self._sessions: dict[tuple[int, SessionKind], Session] = {}
        self._locks: dict[tuple[int, SessionKind], asyncio.Lock] = {}
        self.max_idle_s = max_idle_s
        self.max_sessions = max_sessions

    # --- registration --------------------------------------------------------

    def register_factory(self, kind: SessionKind, factory: SessionFactory) -> None:
        """Teach the registry how to build one kind of session. Called by the
        integration modules at startup."""
        self._factories[kind] = factory

    def has_factory(self, kind: SessionKind) -> bool:
        return kind in self._factories

    # --- access --------------------------------------------------------------

    async def get(self, rt: Any, tenant_id: int, kind: SessionKind) -> Any:
        """The tenant's live client for ``kind``, building it on first use.

        Concurrent callers for the same (tenant, kind) are serialised so a
        tenant can never end up with two competing platform sessions — which for
        WhatsApp in particular would fight over the same linked device.
        """
        if tenant_id <= 0:
            raise ValueError(f"invalid tenant_id: {tenant_id!r}")
        key = (tenant_id, kind)
        existing = self._sessions.get(key)
        if existing is not None:
            existing.touch()
            return existing.client

        factory = self._factories.get(kind)
        if factory is None:
            raise LookupError(
                f"no session factory registered for {kind!r} "
                f"(known: {sorted(self._factories) or 'none'})"
            )

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(key)  # another waiter may have built it
            if existing is not None:
                existing.touch()
                return existing.client
            client = factory(rt, tenant_id)
            if asyncio.iscoroutine(client) or isinstance(client, Awaitable):
                client = await client  # type: ignore[misc]
            self._sessions[key] = Session(tenant_id=tenant_id, kind=kind, client=client)
            await self._enforce_capacity()
            return client

    def peek(self, tenant_id: int, kind: SessionKind) -> Any | None:
        """The cached client without building one (None if not live)."""
        session = self._sessions.get((tenant_id, kind))
        return session.client if session else None

    # --- lifecycle -----------------------------------------------------------

    async def evict(self, tenant_id: int, kind: SessionKind | None = None) -> int:
        """Close and drop a tenant's session(s). Returns how many were closed.

        Call on logout, integration unlink, or account disable — a revoked
        tenant must not keep a live platform connection.
        """
        keys = [
            k for k in list(self._sessions)
            if k[0] == tenant_id and (kind is None or k[1] == kind)
        ]
        for key in keys:
            await self._close(self._sessions.pop(key))
            self._locks.pop(key, None)
        return len(keys)

    async def sweep(self, now: float | None = None) -> int:
        """Close sessions idle beyond ``max_idle_s``. Returns how many."""
        now = now if now is not None else time.time()
        stale = [
            k for k, s in list(self._sessions.items())
            if now - s.last_used_at > self.max_idle_s
        ]
        for key in stale:
            await self._close(self._sessions.pop(key))
            self._locks.pop(key, None)
        return len(stale)

    async def close_all(self) -> int:
        count = len(self._sessions)
        for key in list(self._sessions):
            await self._close(self._sessions.pop(key))
        self._locks.clear()
        return count

    async def _enforce_capacity(self) -> None:
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.items(), key=lambda kv: kv[1].last_used_at)[0]
            await self._close(self._sessions.pop(oldest))
            self._locks.pop(oldest, None)

    async def _close(self, session: Session) -> None:
        """Best-effort close. A client that cannot be closed must not block
        eviction — the alternative is leaking a live session forever."""
        closer = getattr(session.client, "close", None) or getattr(
            session.client, "disconnect", None
        )
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — eviction is best-effort by design
            pass

    # --- introspection -------------------------------------------------------

    @property
    def live_count(self) -> int:
        return len(self._sessions)

    def tenants(self) -> set[int]:
        return {tenant_id for tenant_id, _ in self._sessions}

    def stats(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for _, kind in self._sessions:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        return {
            "live": len(self._sessions),
            "tenants": len(self.tenants()),
            "by_kind": by_kind,
            "factories": sorted(self._factories),
        }
