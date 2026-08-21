"""WhatsApp tools. Sends respect the per-chat send policy; inbound-scope
sends may only target the originating chat."""

from __future__ import annotations

import json
from typing import Any

from ..db import repo
from ..pipeline import confirm
from ..platforms.whatsapp import sender
from ..runtime import Runtime
from .registry import Registry, ToolContext


async def _send_executor(rt: Runtime, payload: dict[str, Any], store) -> str:
    if payload.get("image_path"):
        msg_id = await sender.send_image(rt, payload["chat_jid"], payload["image_path"],
                                         payload.get("text"))
    else:
        msg_id = await sender.send_text(rt, payload["chat_jid"], payload["text"])
    return f"WhatsApp message to {payload.get('chat_name') or payload['chat_jid']} sent ({msg_id})"


confirm.register_executor("wa.send", _send_executor)


async def _send_or_confirm(ctx: ToolContext, chat_jid: str, text: str | None,
                           image_path: str | None = None) -> str:
    rt = ctx.rt
    row = repo.chat_get(ctx.store, "wa", chat_jid)
    chat_name = row["name"] if row else chat_jid

    if ctx.scope == "inbound":
        origin = ctx.extras.get("chat_id")
        if origin != chat_jid or ctx.extras.get("platform") != "wa":
            return json.dumps({"error": "inbound runs may only send to the originating chat"})

    policy = row["send_policy"] if row else "confirm"
    payload = {"chat_jid": chat_jid, "text": text, "image_path": image_path,
               "chat_name": chat_name}

    # Free-send chats with a delay policy: queue the reply instead of sending
    # now — the scheduler fires it (looks natural, and gives time to cancel).
    if policy == "free" and ctx.scope == "inbound" and row is not None and text:
        from ..scheduler.delays import compute_due

        due = compute_due(row["delay_policy_json"])
        if due is not None:
            repo.pending_reply_create(
                ctx.store, chat_pk=row["id"], draft_text=text,
                due_at=due.strftime("%Y-%m-%d %H:%M:%S"))
            return json.dumps({"status": "queued_delayed",
                               "due_utc": due.isoformat(timespec="seconds")})

    if policy == "confirm":
        preview = (text or f"[image {image_path}]")[:600]
        action_id = await confirm.request_confirmation(
            rt, kind="wa.send", payload=payload,
            description=f"WhatsApp → {chat_name}\n\n{preview}",
            chat_pk=row["id"] if row else None,
        )
        return json.dumps({"status": "pending_owner_confirmation", "action_id": action_id})
    result = await _send_executor(rt, payload)
    return json.dumps({"status": "sent", "detail": result})


def register(registry: Registry) -> None:
    @registry.tool(
        "wa_send_message",
        "Send a WhatsApp text message to a chat (by JID from wa_list_groups / "
        "chat_list). Respects the chat's send policy (may require confirmation).",
        {
            "type": "object",
            "properties": {
                "chat_jid": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["chat_jid", "text"],
        },
        scopes=("owner", "inbound"),
        sensitive=True,
    )
    async def wa_send_message(ctx: ToolContext, chat_jid: str, text: str) -> str:
        return await _send_or_confirm(ctx, chat_jid, text)

    @registry.tool(
        "wa_send_image",
        "Send an image (a local file path from attach_image_from_url or a "
        "downloaded media path) to a WhatsApp chat, with an optional caption.",
        {
            "type": "object",
            "properties": {
                "chat_jid": {"type": "string"},
                "image_path": {"type": "string"},
                "caption": {"type": "string"},
            },
            "required": ["chat_jid", "image_path"],
        },
        sensitive=True,
    )
    async def wa_send_image(ctx: ToolContext, chat_jid: str, image_path: str,
                            caption: str = "") -> str:
        return await _send_or_confirm(ctx, chat_jid, caption or None, image_path)

    @registry.tool(
        "wa_list_groups",
        "List WhatsApp groups Archon knows (JID, name, whitelist status). Use "
        "this to resolve approximate group names.",
        scopes=("owner",),
    )
    async def wa_list_groups(ctx: ToolContext) -> str:
        rows = repo.chat_list(ctx.store, platform="wa")
        return json.dumps([
            {"jid": r["chat_id"], "name": r["name"], "kind": r["kind"],
             "whitelisted": bool(r["is_whitelisted"])}
            for r in rows if r["kind"] == "group"
        ], ensure_ascii=False)

    @registry.tool(
        "wa_get_history",
        "Recent cached messages of a WhatsApp chat (Archon's own cache).",
        {
            "type": "object",
            "properties": {
                "chat_jid": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["chat_jid"],
        },
        scopes=("owner", "inbound"),
    )
    async def wa_get_history(ctx: ToolContext, chat_jid: str, limit: int = 30) -> str:
        row = repo.chat_get(ctx.store, "wa", chat_jid)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        if ctx.scope == "inbound" and ctx.extras.get("chat_id") != chat_jid:
            return json.dumps({"error": "inbound runs may only read the originating chat"})
        rows = repo.message_history(ctx.store, row["id"], min(int(limit), 100))
        return json.dumps([
            {"from": "me" if r["is_from_me"] else (r["sender_name"] or r["sender_id"]),
             "ts": r["ts"], "text": r["text"],
             "deleted": bool(r["deleted_at"]), "edited": bool(r["edited_at"])}
            for r in reversed(rows)
        ], ensure_ascii=False)

    @registry.tool(
        "wa_mark_read",
        "Mark recent messages of a WhatsApp chat as read.",
        {
            "type": "object",
            "properties": {"chat_jid": {"type": "string"}},
            "required": ["chat_jid"],
        },
        sensitive=True,
    )
    async def wa_mark_read(ctx: ToolContext, chat_jid: str) -> str:
        row = repo.chat_get(ctx.store, "wa", chat_jid)
        if row is None:
            return json.dumps({"error": "unknown chat"})
        rows = repo.message_history(ctx.store, row["id"], 10)
        ids = [r["msg_id"] for r in rows if not r["is_from_me"]]
        if ids:
            await sender.mark_read(ctx.rt, chat_jid, ids)
        return json.dumps({"ok": True, "marked": len(ids)})

    @registry.tool(
        "wa_check_number",
        "Check whether a phone number (international format) is on WhatsApp.",
        {
            "type": "object",
            "properties": {"phone": {"type": "string"}},
            "required": ["phone"],
        },
    )
    async def wa_check_number(ctx: ToolContext, phone: str) -> str:
        on = await sender.check_number(ctx.rt, phone)
        return json.dumps({"phone": phone, "on_whatsapp": on})
