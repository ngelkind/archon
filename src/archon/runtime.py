"""Shared runtime container passed to every subsystem.

PROCESS-GLOBAL ONLY. Everything here is shared by every tenant: config, the one
lock-guarded database handle, the LLM router, the tool registry, the event hub,
and the per-tenant session registry (global object, per-tenant contents).

Anything that belongs to *one user* — their chats, settings, integration
sessions, agent memory — lives behind a ``tenant.TenantContext``, not here.
``rt.clients`` is the single-user bot's own platform clients (tenant 1); the
product path resolves clients through ``rt.sessions`` instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .bus import Bus
from .config import Settings
from .db import Db
from .events import EventHub
from .logging_.audit import AuditLog
from .sessions import SessionRegistry


@dataclass
class Runtime:
    settings: Settings
    db: Db
    audit: AuditLog
    bus: Bus
    # Best-effort realtime fan-out for API subscribers. Never blocks publishers
    # (see events.py); safe to publish to from any hot path.
    events: EventHub = field(default_factory=EventHub)
    # Per-tenant integration clients, built lazily and evicted when idle.
    sessions: SessionRegistry = field(default_factory=SessionRegistry)
    started_at: float = field(default_factory=time.time)
    # Subsystem health, shown by /status: name -> short state string.
    health: dict[str, str] = field(default_factory=dict)
    # Owner-alert bookkeeping (alerts.py): last-sent per key + pre-bot queue.
    alert_state: dict = field(default_factory=dict)
    # Out-of-band send pacing (logging_/send.py); built lazily inside the loop.
    send_throttle: object | None = None
    # Wired in app.build_runtime after construction (circular-import avoidance):
    # llm.router.Router, tools.registry.Registry, and the control-bot text
    # handler. Typed as Any deliberately.
    router: object | None = None
    registry: object | None = None
    owner_text_handler: object | None = None
    # Platform clients, set by their subsystems when connected (M3+).
    clients: dict[str, object] = field(default_factory=dict)
    # Outbound send budgets for per-tenant userbots (pacing.OutboundPacer).
    # Process-wide by necessity: the per-tenant limits are the smaller half, and
    # the GLOBAL one is what protects the shared Telegram api_id. Built lazily
    # by pacing.pacer_for; typed as Any to avoid a circular import.
    pacer: object | None = None

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at)

    def send_bot(self):
        """Bot instance for out-of-band sends (log cards, capture, alerts).
        Prefers the dedicated notifier (open session) over the polling bot."""
        return self.clients.get("notifier") or self.clients.get("control_bot")
