"""Sub-bot management tools (paste-token flow; BotFather creation is manual)."""

from __future__ import annotations

import json

from ..db import repo
from .registry import Registry, ToolContext


def register(registry: Registry) -> None:
    @registry.tool(
        "subbot_register",
        "Register a new sub-bot: the owner creates a bot in @BotFather, then "
        "pastes its token here with the platform it should be scoped to. The "
        "sub-bot starts polling within ~30s.",
        {
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "platform_scope": {"type": "string", "enum": ["wa", "tg", "gmail"]},
            },
            "required": ["token", "platform_scope"],
        },
        sensitive=True,
    )
    async def subbot_register(ctx: ToolContext, token: str, platform_scope: str) -> str:
        from aiogram import Bot

        token = token.strip()
        bot = Bot(token=token)
        try:
            me = await bot.get_me()  # validates the token against Telegram
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"token rejected by Telegram: {type(exc).__name__}"})
        finally:
            await bot.session.close()
        repo.sub_bot_create(ctx.rt.db, token=token,
                            bot_username=me.username or str(me.id),
                            platform_scope=platform_scope)
        return json.dumps({"ok": True, "bot": f"@{me.username}",
                           "platform_scope": platform_scope,
                           "note": "starts polling within ~30s"})

    @registry.tool(
        "subbot_list",
        "List registered sub-bots.",
    )
    async def subbot_list(ctx: ToolContext) -> str:
        rows = repo.sub_bot_list(ctx.rt.db)
        return json.dumps(
            [{k: r[k] for k in ("id", "bot_username", "platform_scope", "enabled",
                                "created_at")} for r in rows],
            ensure_ascii=False, default=str)

    @registry.tool(
        "subbot_set_enabled",
        "Enable or disable a sub-bot by id.",
        {
            "type": "object",
            "properties": {
                "subbot_id": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            "required": ["subbot_id", "enabled"],
        },
        sensitive=True,
    )
    async def subbot_set_enabled(ctx: ToolContext, subbot_id: int, enabled: bool) -> str:
        ok = repo.sub_bot_set_enabled(ctx.rt.db, subbot_id, enabled)
        return json.dumps({"ok": ok, "id": subbot_id, "enabled": enabled})

    @registry.tool(
        "subbot_remove",
        "Remove a sub-bot registration entirely (its token is deleted).",
        {
            "type": "object",
            "properties": {"subbot_id": {"type": "integer"}},
            "required": ["subbot_id"],
        },
        sensitive=True,
    )
    async def subbot_remove(ctx: ToolContext, subbot_id: int) -> str:
        ok = repo.sub_bot_delete(ctx.rt.db, subbot_id)
        return json.dumps({"ok": ok, "id": subbot_id})
