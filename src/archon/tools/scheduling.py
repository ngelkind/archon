"""Scheduling & delay tools."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from ..db import repo
from ..platforms.telegram import userbot
from ..scheduler.delays import parse_policy
from .registry import Registry, ToolContext


def _to_utc_str(iso: str, tz: str) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def register(registry: Registry) -> None:
    @registry.tool(
        "schedule_message",
        "Schedule a message for later. Telegram groups use Telegram's NATIVE "
        "scheduler (visible in the owner's app); WhatsApp / Telegram private / "
        "email use Archon's scheduler. Times are owner-local unless offset given.",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg", "gmail"]},
                "chat_id": {"type": "string",
                            "description": "JID / tg chat id / email address"},
                "text": {"type": "string"},
                "due_iso": {"type": "string", "description": "e.g. 2026-08-22T09:00:00"},
                "image_path": {"type": "string",
                               "description": "optional local image (attach_image_from_url)"},
                "subject": {"type": "string", "description": "email only"},
            },
            "required": ["platform", "chat_id", "text", "due_iso"],
        },
        sensitive=True,
    )
    async def schedule_message(ctx: ToolContext, platform: str, chat_id: str,
                               text: str, due_iso: str, image_path: str = "",
                               subject: str = "") -> str:
        rt = ctx.rt
        tz = rt.settings.timezone
        kind = "group" if (platform == "tg" and chat_id.startswith("-")) else None

        if platform == "tg" and kind == "group":
            # Native Telegram scheduling via the userbot.
            due = datetime.fromisoformat(due_iso)
            if due.tzinfo is None:
                due = due.replace(tzinfo=ZoneInfo(tz))
            msg_id = await userbot.send_as_owner(rt, chat_id, text, schedule=due)
            chat_pk = repo.chat_upsert(ctx.store, "tg", chat_id, None, "group")
            repo.schedule_create(
                ctx.store, platform="tg", chat_pk=chat_pk, text=text,
                due_at=_to_utc_str(due_iso, tz), status="delegated_native",
                tg_native_id=int(msg_id),
            )
            return json.dumps({"status": "delegated_native", "tg_msg_id": msg_id})

        chat_kind = "email" if platform == "gmail" else (
            "group" if chat_id.endswith("@g.us") else "private")
        chat_pk = repo.chat_upsert(ctx.store, platform, chat_id, None, chat_kind)
        result_extra = json.dumps({"subject": subject}) if subject else None
        sched_id = repo.schedule_create(
            ctx.store, platform=platform, chat_pk=chat_pk, text=text,
            media_path=image_path or None, due_at=_to_utc_str(due_iso, tz),
            result=result_extra,
        )
        return json.dumps({"status": "scheduled", "id": sched_id,
                           "due_utc": _to_utc_str(due_iso, tz)})

    @registry.tool(
        "schedule_list",
        "List scheduled messages (pending and recently sent/failed).",
    )
    async def schedule_list(ctx: ToolContext) -> str:
        rows = repo.schedule_list(ctx.store, limit=30)
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

    @registry.tool(
        "schedule_cancel",
        "Cancel a pending scheduled message by id (from schedule_list). Native "
        "Telegram schedules must be cancelled in the Telegram app itself.",
        {
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
        sensitive=True,
    )
    async def schedule_cancel(ctx: ToolContext, schedule_id: int) -> str:
        ok = repo.schedule_cancel(ctx.store, schedule_id)
        return json.dumps({"ok": ok, "id": schedule_id})

    @registry.tool(
        "attach_image_from_url",
        "Download an image from a URL into local storage; returns a local path "
        "usable with wa_send_image / schedule_message.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        sensitive=True,
    )
    async def attach_image_from_url(ctx: ToolContext, url: str) -> str:
        if not re.match(r"^https?://", url):
            return json.dumps({"error": "only http(s) URLs"})
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}"})
        ctype = resp.headers.get("content-type", "").split(";")[0]
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
               "image/gif": ".gif"}.get(ctype)
        if not ext or len(resp.content) > 10_000_000:
            return json.dumps({"error": f"not a supported image ({ctype}, "
                                        f"{len(resp.content)} bytes)"})
        ctx.rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        path = ctx.rt.settings.media_dir / f"dl-{stamp}{ext}"
        path.write_bytes(resp.content)
        return json.dumps({"ok": True, "path": str(path), "bytes": len(resp.content)})

    @registry.tool(
        "delay_policy_set",
        "Set a chat's auto-reply delay: none, fixed seconds, or a random window "
        "(e.g. reply between 1h and 5h later to look natural).",
        {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["wa", "tg"]},
                "chat_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["none", "fixed", "random"]},
                "min_s": {"type": "number"},
                "max_s": {"type": "number"},
            },
            "required": ["platform", "chat_id", "mode"],
        },
        sensitive=True,
    )
    async def delay_policy_set(ctx: ToolContext, platform: str, chat_id: str,
                               mode: str, min_s: float = 0, max_s: float = 0) -> str:
        if mode not in ("none", "fixed", "random"):
            # parse_policy silently degrades an unknown mode to "none", so the
            # stored row and the reported policy disagreed. Reject it up front.
            return json.dumps({"error": "mode must be one of none|fixed|random"})
        row = repo.chat_get(ctx.store, platform, chat_id)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        policy = None if mode == "none" else json.dumps(
            {"mode": mode, "min_s": min_s, "max_s": max_s or min_s})
        repo.chat_set_field(ctx.store, row["id"], "delay_policy_json", policy)
        return json.dumps({"ok": True, "chat": row["name"] or chat_id,
                           "policy": parse_policy(policy)})

    @registry.tool(
        "pending_replies_list",
        "List queued (delayed) auto-replies.",
    )
    async def pending_replies_list(ctx: ToolContext) -> str:
        rows = repo.pending_reply_list(ctx.store, limit=30)
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

    @registry.tool(
        "pending_reply_cancel",
        "Cancel a queued delayed reply by id.",
        {
            "type": "object",
            "properties": {"reply_id": {"type": "integer"}},
            "required": ["reply_id"],
        },
        sensitive=True,
    )
    async def pending_reply_cancel(ctx: ToolContext, reply_id: int) -> str:
        ok = repo.pending_reply_cancel(ctx.store, reply_id)
        return json.dumps({"ok": ok, "id": reply_id})
