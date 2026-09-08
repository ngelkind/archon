"""One-time media capture tools — a list of chats where view-once / self-
destruct photos, audio and files are downloaded and logged. Separate from the
whitelist. 'All DMs' is a single global toggle."""

from __future__ import annotations

import json

from ..db import repo
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "capture_all_dms_set",
        "Turn one-time media capture on/off for ALL private chats at once. When "
        "on, every view-once photo/audio/file anyone sends you in a DM is saved "
        "to the log channel.",
        {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
        sensitive=True,
    )
    async def capture_all_dms_set(ctx: ToolContext, enabled: bool) -> str:
        repo.setting_set(ctx.store, "capture.all_dms", bool(enabled))
        return json.dumps({"ok": True, "capture_all_dms": bool(enabled)})

    @registry.tool(
        "capture_add",
        "Enable one-time media capture for a specific chat (works for groups "
        "too, or to capture one DM while the global all-DMs toggle is off).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
        sensitive=True,
    )
    async def capture_add(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = repo.chat_get(ctx.store, platform, chat_id)
        if row is None:
            # Fabricating a row for any string armed a chat that never fired and
            # reported ok:true. Require the chat to exist (resolve with chat_find
            # / wa_list_groups first).
            return json.dumps({"error": f"unknown chat {platform}:{chat_id} — "
                               "resolve it with chat_find or wa_list_groups first"})
        pk = row["id"]
        repo.chat_set_field(ctx.store, pk, "capture_media", 1)
        return json.dumps({"ok": True, "chat": (row["name"] if row else chat_id),
                           "capture": True})

    @registry.tool(
        "capture_remove",
        "Disable one-time media capture for a specific chat.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
            },
            "required": ["platform", "chat_id"],
        },
        sensitive=True,
    )
    async def capture_remove(ctx: ToolContext, platform: str, chat_id: str) -> str:
        row = repo.chat_get(ctx.store, platform, chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        repo.chat_set_field(ctx.store, row["id"], "capture_media", 0)
        return json.dumps({"ok": True, "chat": row["name"] or chat_id, "capture": False})

    @registry.tool(
        "capture_list",
        "Show one-time media capture state: the global all-DMs toggle and the "
        "list of individually-armed chats.",
    )
    async def capture_list(ctx: ToolContext) -> str:
        rows = repo.chat_list_capture_armed(ctx.store)
        return json.dumps({
            "all_dms": repo.setting_get(ctx.store, "capture.all_dms", False),
            "armed_chats": [
                {"platform": r["platform"], "chat_id": r["chat_id"],
                 "name": r["name"], "kind": r["kind"]}
                for r in rows
            ],
        }, ensure_ascii=False)
