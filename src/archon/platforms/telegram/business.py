"""Telegram Business connection: private-chat ingest via the control bot.

The owner connects the bot in Settings → Telegram Business → Chatbots. From
then on the bot receives business_message / edited_business_message /
deleted_business_messages for the owner's PRIVATE chats (both directions) and
can send as the owner via business_connection_id. Groups are the userbot's
job (partition rule; see dedupe note in the plan).
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from ...db import repo
from ...models import InboundMessage, MediaRef
from ...runtime import Runtime


def _chat_name(message: Message) -> str | None:
    chat = message.chat
    name = " ".join(x for x in [chat.first_name, chat.last_name] if x) or chat.title
    return name or (f"@{chat.username}" if chat.username else None)


async def _download_photo_if_wanted(rt: Runtime, bot: Bot, message: Message,
                                    inbound: InboundMessage) -> None:
    if not message.photo:
        return
    row = repo.chat_get(rt.db, "tg", inbound.chat_id)
    if not row or not row["image_recognition"] or not row["is_whitelisted"]:
        return
    try:
        # Largest variant under ~5MB
        variants = sorted(message.photo, key=lambda p: p.file_size or 0)
        pick = next((p for p in reversed(variants) if (p.file_size or 0) < 5_000_000), None)
        if pick is None:
            return
        rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
        path = rt.settings.media_dir / f"tg-{message.chat.id}-{message.message_id}.jpg"
        await bot.download(pick.file_id, destination=str(path))
        inbound.media.append(MediaRef(kind="image", local_path=str(path), mime="image/jpeg"))
    except Exception as exc:  # noqa: BLE001
        rt.audit.note("tg_photo_download_failed", error=repr(exc)[:200])


def _to_inbound(rt: Runtime, message: Message, *, is_edit: bool = False) -> InboundMessage:
    sender = message.from_user
    is_from_me = bool(sender and sender.id == rt.settings.telegram_owner_id)
    text = message.text or message.caption
    return InboundMessage(
        platform="tg",
        source="business",
        chat_id=str(message.chat.id),
        chat_kind="private",
        chat_name=_chat_name(message),
        msg_id=str(message.message_id),
        sender_id=str(sender.id) if sender else "unknown",
        sender_name=(sender.full_name if sender else None),
        ts=message.date or datetime.now(UTC),
        is_from_me=is_from_me,
        text=text,
        is_edit=is_edit,
        business_connection_id=message.business_connection_id,
        raw={"has_photo": bool(message.photo)},
    )


def register(dp: Dispatcher, rt: Runtime) -> None:
    @dp.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        enabled = getattr(connection, "is_enabled", None)
        if enabled is None:
            rights = getattr(connection, "rights", None)
            enabled = rights is not None
        repo.setting_set(rt.db, "tg.business_connection_id",
                         connection.id if enabled else None)
        rt.audit.note("tg_business_connection", id=connection.id, enabled=bool(enabled))
        rt.health["tg_business"] = "connected" if enabled else "disconnected"
        bot: Bot | None = rt.clients.get("control_bot")  # type: ignore[assignment]
        if bot:
            state = "connected ✅" if enabled else "disconnected ❌"
            try:
                await bot.send_message(rt.settings.telegram_owner_id,
                                       f"Telegram Business connection {state}")
            except Exception:  # noqa: BLE001
                pass

    @dp.business_message()
    async def on_business_message(message: Message) -> None:
        # Owner-issued /download in a private chat: delete + re-send as owner.
        from . import download_cmd

        is_from_me = bool(message.from_user
                          and message.from_user.id == rt.settings.telegram_owner_id)
        if is_from_me and message.business_connection_id:
            url = download_cmd.is_download_command(message.text)
            if url:
                await download_cmd.handle_business(
                    rt, message.bot, message.chat.id, message.message_id,
                    message.business_connection_id, url)
                return

        inbound = _to_inbound(rt, message)
        bot: Bot | None = rt.clients.get("control_bot")  # type: ignore[assignment]
        if bot:
            await _download_photo_if_wanted(rt, bot, message, inbound)
        await rt.bus.publish(inbound)

    @dp.edited_business_message()
    async def on_edited_business_message(message: Message) -> None:
        await rt.bus.publish(_to_inbound(rt, message, is_edit=True))

    @dp.deleted_business_messages()
    async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
        for msg_id in event.message_ids:
            await rt.bus.publish(InboundMessage(
                platform="tg",
                source="business",
                chat_id=str(event.chat.id),
                chat_kind="private",
                chat_name=event.chat.full_name or event.chat.title,
                msg_id=str(msg_id),
                sender_id="unknown",
                ts=datetime.now(UTC),
                is_delete=True,
            ))
