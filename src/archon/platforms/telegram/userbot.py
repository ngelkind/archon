"""Telethon userbot: the owner's own account session.

Covers what Business connections cannot: group/channel reading (whitelist),
the full dialog list (whitelist-by-approximate-name), group deletion/edit
logging, native scheduled messages, and sending as the owner in groups.

Partition rule: the userbot IGNORES private chats entirely — those arrive via
the Business connection. Belt-and-suspenders: the messages-table unique index
would drop duplicates anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from ...db import repo
from ...models import InboundMessage, MediaRef
from ...runtime import Runtime


def _display_name(entity: Any) -> str | None:
    for attr in ("title",):
        if getattr(entity, attr, None):
            return getattr(entity, attr)
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or getattr(entity, "username", None)


def _norm_chat_id(chat_id: int) -> str:
    # Telethon marks channels/supergroups as -100xxxxxxxxxx; keep that form —
    # it matches what get_dialogs and Bot API use.
    return str(chat_id)


def _ephemeral_kind(msg: Any) -> str | None:
    """Return the media kind if the message is self-destruct (ttl), else None."""
    media = getattr(msg, "media", None)
    if media is None or not getattr(media, "ttl_seconds", None):
        return None
    if getattr(msg, "photo", None):
        return "image"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "audio", None):
        return "audio"
    return "document"


async def _sync_dialogs(rt: Runtime, client: TelegramClient) -> int:
    count = 0
    async for dialog in client.iter_dialogs(limit=500):
        if dialog.is_user:
            continue  # private chats belong to the Business partition
        kind = "channel" if (dialog.is_channel and not dialog.is_group) else "group"
        repo.chat_upsert(rt.db, "tg", _norm_chat_id(dialog.id),
                         dialog.name or None, kind)
        count += 1
    return count


async def run(rt: Runtime) -> None:
    s = rt.settings
    if not (s.telegram_api_id and s.telegram_api_hash and s.telethon_session):
        rt.health["tg_userbot"] = "not configured (TELEGRAM_API_ID/HASH/TELETHON_SESSION)"
        return

    client = TelegramClient(
        StringSession(s.telethon_session), s.telegram_api_id, s.telegram_api_hash
    )
    rt.clients["tg_userbot"] = client
    await client.connect()
    if not await client.is_user_authorized():
        rt.health["tg_userbot"] = "SESSION INVALID — re-run scripts/telethon_login.py"
        rt.audit.note("tg_userbot_unauthorized")
        return

    me = await client.get_me()
    rt.audit.note("tg_userbot_started", user=me.username or me.id)
    synced = await _sync_dialogs(rt, client)
    rt.audit.note("tg_dialogs_synced", count=synced)
    rt.health["tg_userbot"] = "connected"

    async def _download_photo(event: Any, inbound: InboundMessage) -> None:
        row = repo.chat_get(rt.db, "tg", inbound.chat_id)
        if not row or not row["image_recognition"] or not row["is_whitelisted"]:
            return
        try:
            rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
            path = rt.settings.media_dir / f"tgu-{inbound.chat_id}-{inbound.msg_id}.jpg"
            saved = await event.message.download_media(file=str(path))
            if saved:
                inbound.media.append(
                    MediaRef(kind="image", local_path=str(saved), mime="image/jpeg"))
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("tg_userbot_media_failed", error=repr(exc)[:200])

    def _to_inbound(event: Any, chat: Any, *, is_edit: bool = False) -> InboundMessage:
        sender = getattr(event, "sender", None)
        sender_id = str(getattr(event, "sender_id", "") or "unknown")
        msg = event.message
        return InboundMessage(
            platform="tg",
            source="userbot",
            chat_id=_norm_chat_id(event.chat_id),
            chat_kind="channel" if getattr(chat, "broadcast", False) else "group",
            chat_name=_display_name(chat) if chat else None,
            msg_id=str(msg.id),
            sender_id=sender_id,
            sender_name=_display_name(sender) if sender else None,
            ts=msg.date or datetime.now(UTC),
            is_from_me=bool(getattr(msg, "out", False)),
            text=msg.message or None,
            is_edit=is_edit,
        )

    async def _capture_ephemeral(event: Any, chat_kind: str) -> None:
        from ...logging_ import capture

        chat_id = _norm_chat_id(event.chat_id)
        if not capture.capture_enabled(rt, "tg", chat_id, chat_kind):
            return
        kind = _ephemeral_kind(event.message)
        try:
            rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
            path = rt.settings.media_dir / f"tg-vo-{chat_id}-{event.message.id}"
            saved = await event.message.download_media(file=str(path))
            if not saved:
                return
            chat = await event.get_chat()
            sender = getattr(event, "sender", None)
            await capture.send_capture(
                rt, platform="tg", chat_id=chat_id,
                chat_name=_display_name(chat) if chat else None,
                sender_name=(_display_name(sender) if sender else "unknown"),
                kind=kind or "document", local_path=str(saved))
        except Exception as exc:  # noqa: BLE001
            rt.audit.note("tg_capture_failed", error=repr(exc)[:200])

    @client.on(events.NewMessage())
    async def on_new(event: Any) -> None:
        # One-time (self-destruct) media capture — works in private chats too,
        # which is the ONLY way to get them (Bot API never delivers ttl media).
        if _ephemeral_kind(event.message) and not getattr(event.message, "out", False):
            await _capture_ephemeral(event, "private" if event.is_private else "group")

        if event.is_private:
            return  # Business partition (pipeline); capture handled above
        # Owner-issued /download in a group: delete + re-send as owner (MTProto).
        if getattr(event.message, "out", False):
            from . import download_cmd

            url = download_cmd.is_download_command(event.message.message)
            if url:
                await download_cmd.handle_group_userbot(
                    rt, client, _norm_chat_id(event.chat_id), event.message.id, url)
                return
        chat = await event.get_chat()
        inbound = _to_inbound(event, chat)
        if event.message.photo:
            await _download_photo(event, inbound)
        await rt.bus.publish(inbound)

    @client.on(events.MessageEdited())
    async def on_edit(event: Any) -> None:
        if event.is_private:
            return
        chat = await event.get_chat()
        await rt.bus.publish(_to_inbound(event, chat, is_edit=True))

    @client.on(events.MessageDeleted())
    async def on_delete(event: Any) -> None:
        # Channels/supergroups carry chat_id; legacy chats don't — resolve from
        # the message cache instead of guessing.
        chat_id = _norm_chat_id(event.chat_id) if event.chat_id else None
        for msg_id in event.deleted_ids:
            resolved_chat = chat_id
            if resolved_chat is None:
                row = rt.db.query_one(
                    "SELECT chat_id FROM messages WHERE platform = 'tg' AND msg_id = ? "
                    "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
                    (str(msg_id),),
                )
                resolved_chat = row["chat_id"] if row else None
            if resolved_chat is None:
                continue
            await rt.bus.publish(InboundMessage(
                platform="tg", source="userbot", chat_id=resolved_chat,
                chat_kind="group", msg_id=str(msg_id), sender_id="unknown",
                ts=datetime.now(UTC), is_delete=True,
            ))

    try:
        await client.run_until_disconnected()
    finally:
        rt.health["tg_userbot"] = "disconnected"


# --- helpers used by tools ----------------------------------------------------

async def send_as_owner(rt: Runtime, chat_id: str, text: str,
                        schedule: datetime | None = None) -> str:
    client: TelegramClient | None = rt.clients.get("tg_userbot")  # type: ignore[assignment]
    if client is None:
        raise RuntimeError("Telegram userbot is not connected")
    entity = await client.get_entity(int(chat_id))
    msg = await client.send_message(entity, text, schedule=schedule)
    return str(getattr(msg, "id", "sent"))


async def refresh_dialogs(rt: Runtime) -> int:
    client: TelegramClient | None = rt.clients.get("tg_userbot")  # type: ignore[assignment]
    if client is None:
        raise RuntimeError("Telegram userbot is not connected")
    return await _sync_dialogs(rt, client)
