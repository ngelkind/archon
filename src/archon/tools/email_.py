"""Email tools. Sending goes through the per-chat send policy: 'confirm'
(default) routes through the confirmation gate even for the owner, unless the
owner marked the correspondent's chat as free-send."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..db import repo
from ..db.tenancy import tenant_id_of
from ..pipeline import confirm
from ..platforms.gmail.client import GmailClient, body_text, header, sender_address
from ..runtime import Runtime
from .registry import Registry, ToolContext


async def _client(rt: Runtime, tenant_id: int) -> GmailClient:
    """The calling tenant's Gmail client (the owner's file token for tenant 1
    until they link through the OAuth flow)."""
    from ..integrations import google as google_integration

    return await google_integration.client_for(rt, tenant_id, "gmail")


async def _send_executor(rt: Runtime, payload: dict[str, Any], store) -> str:
    msg_id = await asyncio.to_thread(
        (await _client(rt, tenant_id_of(store))).send,
        to=payload["to"], subject=payload["subject"], body=payload["body"],
        attachment_path=payload.get("attachment_path"),
        thread_id=payload.get("thread_id"),
        in_reply_to=payload.get("in_reply_to"),
    )
    chat_pk = repo.chat_upsert(store, "gmail", payload["to"].lower(), payload["to"],
                               "email")
    repo.message_cache_outgoing(
        store, chat_pk=chat_pk, platform="gmail", chat_id=payload["to"].lower(),
        msg_id=msg_id, source="gmail",
        text=f"Subject: {payload['subject']}\n\n{payload['body']}",
    )
    return f"email to {payload['to']} sent (id {msg_id})"


confirm.register_executor("email.send", _send_executor)


def _policy_for(store, address: str) -> str:
    row = repo.chat_get(store, "gmail", address.lower())
    return row["send_policy"] if row else "confirm"


async def _send_or_confirm(ctx: ToolContext, payload: dict[str, Any]) -> str:
    if ctx.scope == "inbound" or _policy_for(ctx.store, payload["to"]) == "confirm":
        action_id = await confirm.request_confirmation(
            ctx.rt, kind="email.send", payload=payload,
            description=f"To: {payload['to']}\nSubject: {payload['subject']}\n\n"
                        f"{payload['body'][:800]}",
            chat_pk=ctx.origin_chat_pk, tenant=ctx.tenant,
        )
        return json.dumps({"status": "pending_owner_confirmation", "action_id": action_id})
    result = await _send_executor(ctx.rt, payload, ctx.store)
    return json.dumps({"status": "sent", "detail": result})


def register(registry: Registry) -> None:
    @registry.tool(
        "email_send",
        "Send a new email. Depending on the recipient's send policy this may "
        "require the owner's one-tap confirmation.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachment_path": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        scopes=("owner",),
        sensitive=True,
    )
    async def email_send(ctx: ToolContext, **kwargs: Any) -> str:
        return await _send_or_confirm(ctx, {k: v for k, v in kwargs.items() if v})

    @registry.tool(
        "email_reply",
        "Reply within an existing email thread (get gmail_msg_id from email_read).",
        {
            "type": "object",
            "properties": {
                "gmail_msg_id": {"type": "string",
                                 "description": "id of the message being replied to"},
                "body": {"type": "string"},
            },
            "required": ["gmail_msg_id", "body"],
        },
        scopes=("owner", "inbound"),
        sensitive=True,
    )
    async def email_reply(ctx: ToolContext, gmail_msg_id: str, body: str) -> str:
        client = await _client(ctx.rt, ctx.tenant_id)
        original = await asyncio.to_thread(client.get_message, gmail_msg_id)
        to = sender_address(original)
        subject = header(original, "Subject")
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        payload = {
            "to": to, "subject": subject or "Re:", "body": body,
            "thread_id": original.get("threadId"),
            "in_reply_to": header(original, "Message-ID") or None,
        }
        return await _send_or_confirm(ctx, {k: v for k, v in payload.items() if v})

    @registry.tool(
        "email_search",
        "Search Gmail with a standard Gmail query (e.g. 'from:dana newer_than:7d').",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        scopes=("owner",),
    )
    async def email_search(ctx: ToolContext, query: str, limit: int = 10) -> str:
        client = await _client(ctx.rt, ctx.tenant_id)
        stubs = await asyncio.to_thread(client.list_messages,
                                        query=query, limit=min(int(limit), 25))
        out = []
        for stub in stubs:
            msg = await asyncio.to_thread(client.get_message, stub["id"])
            out.append({
                "id": stub["id"],
                "from": sender_address(msg),
                "subject": header(msg, "Subject"),
                "date": header(msg, "Date"),
                "snippet": (msg.get("snippet") or "")[:200],
            })
        return json.dumps(out, ensure_ascii=False)

    @registry.tool(
        "email_read",
        "Read the full body of an email by id.",
        {
            "type": "object",
            "properties": {"gmail_msg_id": {"type": "string"}},
            "required": ["gmail_msg_id"],
        },
        scopes=("owner",),
    )
    async def email_read(ctx: ToolContext, gmail_msg_id: str) -> str:
        client = await _client(ctx.rt, ctx.tenant_id)
        msg = await asyncio.to_thread(client.get_message, gmail_msg_id)
        return json.dumps({
            "id": gmail_msg_id,
            "from": sender_address(msg),
            "subject": header(msg, "Subject"),
            "date": header(msg, "Date"),
            "body": body_text(msg)[:8000],
            "threadId": msg.get("threadId"),
        }, ensure_ascii=False)

    @registry.tool(
        "email_mark_read",
        "Mark an email as read.",
        {
            "type": "object",
            "properties": {"gmail_msg_id": {"type": "string"}},
            "required": ["gmail_msg_id"],
        },
    )
    async def email_mark_read(ctx: ToolContext, gmail_msg_id: str) -> str:
        await asyncio.to_thread((await _client(ctx.rt, ctx.tenant_id)).mark_read, gmail_msg_id)
        return json.dumps({"ok": True})
