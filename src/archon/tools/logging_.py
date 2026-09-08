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
        raw = str(channel_id).strip()  # a JSON number would crash .strip()
        if not raw:
            repo.setting_set(ctx.store, "log.channel_id", None)
            return json.dumps({"ok": True, "log_channel_id": None})
        try:
            value = int(raw)
        except ValueError:
            return json.dumps({"error": "channel_id must be a numeric id like -1001234567890"})
        repo.setting_set(ctx.store, "log.channel_id", value)
        return json.dumps({"ok": True, "log_channel_id": value})

    @registry.tool(
        "log_channel_test",
        "Send a test card to the configured log channel.",
        sensitive=True,
    )
    async def log_channel_test(ctx: ToolContext) -> str:
        from ..logging_.send import throttled_send

        channel = repo.setting_get(ctx.store, "log.channel_id",
                                   ctx.rt.settings.tg_log_channel_id)
        if not channel:
            return json.dumps({"error": "no log channel configured — set one with log_channel_set"})
        if ctx.rt.send_bot() is None:
            return json.dumps({"error": "the notifier bot is not running"})
        # Go through the throttled sender (own session, rebuilds on failure) —
        # the control-bot handle's aiohttp session is closed by its poll loop.
        result = await throttled_send(
            ctx.rt, lambda b: b.send_message(int(channel), "🧪 Archon log channel test — OK"),
            kind="log_test")
        if result is None:
            return json.dumps({"error": f"could not post to channel {channel} — "
                               "is the bot an admin there?"})
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
        repo.setting_set(ctx.store, "log.redact_pii", bool(enabled))
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
        where = "(deleted_at IS NOT NULL OR edited_at IS NOT NULL)"
        params: list = []
        if platform:
            where += " AND platform = ?"
            params.append(platform)
        where += " ORDER BY id DESC LIMIT ?"
        params.append(min(int(limit), 50))
        rows = repo.message_search(ctx.store, where, tuple(params))
        keep = ("platform", "chat_id", "sender_name", "sender_id", "ts", "text",
                "edited_text", "deleted_at", "edited_at")
        return json.dumps([{k: r[k] for k in keep} for r in rows],
                          ensure_ascii=False, default=str)

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
        rows = repo.audit_query(ctx.store, contains=contains,
                                limit=min(int(limit), 100))
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
