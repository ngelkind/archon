"""Logging-channel and audit tools."""

from __future__ import annotations

import json

from ..db import repo
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "log_channel_set",
        "Set the Telegram channel id for deleted/edited-message cards. The "
        "control bot must be an admin of that channel. Empty disables logging.",
        {
            "type": "object",
            "properties": {"channel_id": {"type": "string",
                                          "description": "e.g. -1001234567890, or empty"}},
            "required": ["channel_id"],
        },
        sensitive=True,
    )
    async def log_channel_set(ctx: ToolContext, channel_id: str) -> str:
        value = int(channel_id) if channel_id.strip() else None
        repo.setting_set(ctx.rt.db, "log.channel_id", value)
        return json.dumps({"ok": True, "log_channel_id": value})

    @registry.tool(
        "log_channel_test",
        "Send a test card to the configured log channel.",
        sensitive=True,
    )
    async def log_channel_test(ctx: ToolContext) -> str:
        channel = repo.setting_get(ctx.rt.db, "log.channel_id",
                                   ctx.rt.settings.tg_log_channel_id)
        bot = ctx.rt.clients.get("control_bot")
        if not channel or bot is None:
            return json.dumps({"error": "no channel configured or bot not running"})
        await bot.send_message(int(channel), "🧪 Archon log channel test — OK")  # type: ignore[attr-defined]
        return json.dumps({"ok": True, "channel": channel})

    @registry.tool(
        "redaction_set",
        "Enable/disable PII redaction (cards in the log channel get card "
        "numbers, IDs, IBANs, API keys masked).",
        {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        sensitive=True,
    )
    async def redaction_set(ctx: ToolContext, enabled: bool) -> str:
        repo.setting_set(ctx.rt.db, "log.redact_pii", bool(enabled))
        return json.dumps({"ok": True, "redact_pii": bool(enabled)})

    @registry.tool(
        "deleted_messages_query",
        "List recently deleted/edited messages from the cache (before-content "
        "included when it was cached).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "limit": {"type": "integer"},
            },
        },
    )
    async def deleted_messages_query(ctx: ToolContext, platform: str = "",
                                     limit: int = 20) -> str:
        sql = ("SELECT platform, chat_id, sender_name, sender_id, ts, text, "
               "edited_text, deleted_at, edited_at FROM messages "
               "WHERE (deleted_at IS NOT NULL OR edited_at IS NOT NULL)")
        params: list = []
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(int(limit), 50))
        rows = ctx.rt.db.query(sql, tuple(params))
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

    @registry.tool(
        "audit_query",
        "Query the audit trail (gate decisions, tool calls, notes).",
        {
            "type": "object",
            "properties": {
                "contains": {"type": "string", "description": "substring filter"},
                "limit": {"type": "integer"},
            },
        },
    )
    async def audit_query(ctx: ToolContext, contains: str = "", limit: int = 30) -> str:
        sql = "SELECT ts, actor, action, detail_json FROM audit"
        params: list = []
        if contains:
            sql += " WHERE action LIKE ? OR detail_json LIKE ?"
            params += [f"%{contains}%", f"%{contains}%"]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(int(limit), 100))
        rows = ctx.rt.db.query(sql, tuple(params))
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
