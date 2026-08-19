"""Shared runtime container passed to every subsystem."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .bus import Bus
from .config import Settings
from .db import Db
from .logging_.audit import AuditLog


@dataclass
class Runtime:
    settings: Settings
    db: Db
    audit: AuditLog
    bus: Bus
    started_at: float = field(default_factory=time.time)
    # Subsystem health, shown by /status: name -> short state string.
    health: dict[str, str] = field(default_factory=dict)
    # Wired in app.build_runtime after construction (circular-import avoidance):
    # llm.router.Router, tools.registry.Registry, and the control-bot text
    # handler. Typed as Any deliberately.
    router: object | None = None
    registry: object | None = None
    owner_text_handler: object | None = None
    # Platform clients, set by their subsystems when connected (M3+).
    clients: dict[str, object] = field(default_factory=dict)

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at)
