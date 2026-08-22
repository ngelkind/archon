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
        self._on_evict: dict[SessionKind, Callable[[Any, int], Any]] = {}
        self._rt: Any = None
        self._sessions: dict[tuple[int, SessionKind], Session] = {}
        self._locks: dict[tuple[int, SessionKind], asyncio.Lock] = {}
        self.max_idle_s = max_idle_s
        self.max_sessions = max_sessions

    # --- registration --------------------------------------------------------

    def register_factory(self, kind: SessionKind, factory: SessionFactory,
                         on_evict: Callable[[Any, int], Any] | None = None) -> None:
        """Teach the registry how to build one kind of session.

        ``on_evict(rt, tenant_id)`` runs just before a session is dropped, for
        integrations that must persist state first — WhatsApp writes its
        linked-device session back encrypted and removes the plaintext file.
        """
        self._factories[kind] = factory
        if on_evict is not None:
            self._on_evict[kind] = on_evict

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
        self._rt = rt          # remembered so eviction hooks have a runtime
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
        eviction — the alternative is leaking a live session forever.

        SHUT THE CLIENT DOWN FIRST, THEN RUN THE HOOK. The hook's job is
        post-eviction work on the session's files (WhatsApp encrypts its
        session.db back into the database), and doing that while the client is
        still running reads a file being written underneath it — a torn read
        that would be stored as a corrupt session.

        ``stop`` is preferred over ``disconnect`` because they are not the same
        thing: neonize's ``disconnect`` closes the websocket but leaves the Go
        client alive, still holding its SQLite handle open. Only ``stop``
        releases it. Evicting with ``disconnect`` left an fd on a session.db
        that was then deleted and recreated, and whatsmeow's next write went to
        the orphaned inode — SQLITE_READONLY_DBMOVED, seen live as "attempt to
        write a readonly database" mid-pairing. Telethon has no ``stop`` and
        falls through to ``disconnect``, which for it is the full teardown.
        """
        closer = (
            getattr(session.client, "stop", None)
            or getattr(session.client, "close", None)
            or getattr(session.client, "disconnect", None)
        )
        if closer is not None:
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — eviction is best-effort by design
                pass

        hook = self._on_evict.get(session.kind)
        if hook is not None and self._rt is not None:
            try:
                result = hook(self._rt, session.tenant_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 — a failed hook must not leak the session
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
