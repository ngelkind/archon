"""Telegram tools: private sends as the owner (Business), group sends via the
userbot, dialog listing, history, native scheduling, owner notifications."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..db import repo
from ..pipeline import confirm
from ..platforms.telegram import userbot
from ..runtime import Runtime
from .registry import Registry, ToolContext


async def _send_private_executor(rt: Runtime, payload: dict[str, Any]) -> str:
    bot = rt.clients.get("control_bot")
    if bot is None:
        raise RuntimeError("control bot is not running")
    conn_id = repo.setting_get(rt.db, "tg.business_connection_id", None)
    if not conn_id:
        raise RuntimeError("no Telegram Business connection (connect the bot in "
                           "Settings → Telegram Business → Chatbots)")
    sent = await bot.send_message(  # type: ignore[attr-defined]
        chat_id=int(payload["chat_id"]),
        text=payload["text"],
        business_connection_id=conn_id,
        parse_mode=None,
    )
    chat_pk = repo.chat_upsert(rt.db, "tg", payload["chat_id"],
                               payload.get("chat_name"), "private")
    rt.db.execute(
        "INSERT OR IGNORE INTO messages (chat_pk, platform, chat_id, msg_id, source, "
        "sender_id, is_from_me, ts, text) VALUES (?, 'tg', ?, ?, 'business', 'me', 1, "
        "datetime('now'), ?)",
        (chat_pk, payload["chat_id"], str(sent.message_id), payload["text"]),
    )
    return f"Telegram message to {payload.get('chat_name') or payload['chat_id']} sent (as you)"


async def _send_group_executor(rt: Runtime, payload: dict[str, Any]) -> str:
    schedule = None
    if payload.get("schedule_iso"):
        schedule = datetime.fromisoformat(payload["schedule_iso"])
        if schedule.tzinfo is None:
            schedule = schedule.replace(tzinfo=ZoneInfo(payload.get("tz", "UTC")))
    msg_id = await userbot.send_as_owner(rt, payload["chat_id"], payload["text"],
                                         schedule=schedule)
    verb = "scheduled" if schedule else "sent"
    return f"Telegram group message {verb} to {payload.get('chat_name') or payload['chat_id']} ({msg_id})"


confirm.register_executor("tg.send_private", _send_private_executor)
confirm.register_executor("tg.send_group", _send_group_executor)


async def _policy_send(ctx: ToolContext, kind: str, payload: dict[str, Any]) -> str:
    rt = ctx.rt
    row = repo.chat_get(rt.db, "tg", payload["chat_id"])
    payload.setdefault("chat_name", row["name"] if row else None)

    if ctx.scope == "inbound":
        if ctx.extras.get("platform") != "tg" or ctx.extras.get("chat_id") != payload["chat_id"]:
            return json.dumps({"error": "inbound runs may only send to the originating chat"})

    policy = row["send_policy"] if row else "confirm"

    if (policy == "free" and ctx.scope == "inbound" and row is not None
            and payload.get("text") and not payload.get("schedule_iso")):
        from ..scheduler.delays import compute_due

        due = compute_due(row["delay_policy_json"])
        if due is not None:
            rt.db.execute(
                "INSERT INTO pending_replies (chat_pk, draft_text, due_at) VALUES (?, ?, ?)",
                (row["id"], payload["text"], due.strftime("%Y-%m-%d %H:%M:%S")),
            )
            return json.dumps({"status": "queued_delayed",
                               "due_utc": due.isoformat(timespec="seconds")})

    if policy == "confirm":
        action_id = await confirm.request_confirmation(
            rt, kind=kind, payload=payload,
            description=f"Telegram → {payload.get('chat_name') or payload['chat_id']}\n\n"
                        f"{payload['text'][:600]}",
            chat_pk=row["id"] if row else None,
        )
        return json.dumps({"status": "pending_owner_confirmation", "action_id": action_id})
    executor = _send_private_executor if kind == "tg.send_private" else _send_group_executor
    result = await executor(rt, payload)
    return json.dumps({"status": "sent", "detail": result})


def register(registry: Registry) -> None:
    @registry.tool(
        "tg_send_private",
        "Send a Telegram message AS THE OWNER in one of their private chats "
        "(via the Business connection). chat_id from chat_list/chat_find.",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["chat_id", "text"],
        },
        scopes=("owner", "inbound"),
        sensitive=True,
    )
    async def tg_send_private(ctx: ToolContext, chat_id: str, text: str) -> str:
        return await _policy_send(ctx, "tg.send_private",
                                  {"chat_id": chat_id, "text": text})

    @registry.tool(
        "tg_send_group",
        "Send a Telegram message as the owner into a group/channel (userbot). "
        "Optional schedule_iso uses Telegram's NATIVE scheduled messages.",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {"type": "string"},
                "schedule_iso": {"type": "string",
                                 "description": "Optional ISO time for native scheduling"},
            },
            "required": ["chat_id", "text"],
        },
        scopes=("owner", "inbound"),
        sensitive=True,
    )
    async def tg_send_group(ctx: ToolContext, chat_id: str, text: str,
                            schedule_iso: str = "") -> str:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "tz": ctx.rt.settings.timezone}
        if schedule_iso:
            payload["schedule_iso"] = schedule_iso
        return await _policy_send(ctx, "tg.send_group", payload)

    @registry.tool(
        "tg_list_dialogs",
        "List the owner's Telegram groups/channels (from the userbot's dialog "
        "sync). Set refresh=true to re-sync from Telegram first.",
        {
            "type": "object",
            "properties": {"refresh": {"type": "boolean"}},
        },
        scopes=("owner",),
    )
    async def tg_list_dialogs(ctx: ToolContext, refresh: bool = False) -> str:
        if refresh:
            count = await userbot.refresh_dialogs(ctx.rt)
        rows = repo.chat_list(ctx.rt.db, platform="tg")
        return json.dumps([
            {"chat_id": r["chat_id"], "name": r["name"], "kind": r["kind"],
             "whitelisted": bool(r["is_whitelisted"])}
            for r in rows if r["kind"] in ("group", "channel")
        ], ensure_ascii=False)

    @registry.tool(
        "tg_get_history",
        "Recent cached messages of a Telegram chat (Archon's own cache).",
        {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["chat_id"],
        },
        scopes=("owner", "inbound"),
    )
    async def tg_get_history(ctx: ToolContext, chat_id: str, limit: int = 30) -> str:
        row = repo.chat_get(ctx.rt.db, "tg", chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        if ctx.scope == "inbound" and ctx.extras.get("chat_id") != chat_id:
            return json.dumps({"error": "inbound runs may only read the originating chat"})
        rows = repo.message_history(ctx.rt.db, row["id"], min(int(limit), 100))
        return json.dumps([
            {"from": "me" if r["is_from_me"] else (r["sender_name"] or r["sender_id"]),
             "ts": r["ts"], "text": r["text"],
             "deleted": bool(r["deleted_at"]), "edited": bool(r["edited_at"])}
            for r in reversed(rows)
        ], ensure_ascii=False)

    @registry.tool(
        "tg_notify_owner",
        "Send a notification to the owner via the control bot.",
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        scopes=("owner", "inbound"),
    )
    async def tg_notify_owner(ctx: ToolContext, text: str) -> str:
        bot = ctx.rt.send_bot()
        if bot is None:
            return json.dumps({"error": "control bot not running"})
        await bot.send_message(ctx.rt.settings.telegram_owner_id, text[:4000],  # type: ignore[attr-defined]
                               parse_mode=None)
        return json.dumps({"ok": True})
