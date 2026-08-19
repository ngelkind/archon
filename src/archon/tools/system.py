"""System tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "system_status",
        "Subsystem health, uptime, and queue depth.",
        scopes=("owner",),
    )
    async def system_status(ctx: ToolContext) -> str:
        rt = ctx.rt
        return json.dumps({
            "uptime_s": rt.uptime_s(),
            "queue_depth": rt.bus.depth,
            "subsystems": rt.health,
        })

    @registry.tool(
        "current_datetime",
        "The current date and time (UTC and local).",
        scopes=("owner", "inbound"),
    )
    async def current_datetime(ctx: ToolContext) -> str:
        now = datetime.now()
        return json.dumps({
            "utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "local": now.astimezone().isoformat(timespec="seconds"),
        })

    @registry.tool(
        "db_backup_now",
        "Create an immediate backup copy of the Archon database.",
        sensitive=True,
    )
    async def db_backup_now(ctx: ToolContext) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        dest = ctx.rt.settings.archon_data / f"backup-{stamp}.db"
        ctx.rt.db.backup_to(dest)
        return json.dumps({"ok": True, "path": str(dest)})

    @registry.tool(
        "help_tools",
        "List every tool available in the current context with its description.",
        scopes=("owner", "inbound"),
    )
    async def help_tools(ctx: ToolContext) -> str:
        registry_: Registry = ctx.rt.registry  # type: ignore[assignment]
        return json.dumps([
            {"name": s.name, "description": s.description}
            for s in registry_.specs_for(ctx.scope)
        ])
