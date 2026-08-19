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

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at)
