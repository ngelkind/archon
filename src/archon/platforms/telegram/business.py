"""Telegram Business connection: private-chat ingest.

A user connects the bot in Settings → Telegram Business → Chatbots. From then
on the bot receives business_message / edited_business_message /
deleted_business_messages for THEIR private chats (both directions) and can
send as them via business_connection_id. Groups are the userbot's job
(partition rule; see dedupe note in the plan).

Single-user: the owner is the only connector, and everything lands on tenant 1.
Product: many users connect the same bot, so every update is routed by its
``business_connection_id`` to the tenant that owns it — a message whose
connection resolves to no tenant is dropped rather than guessed at, since
guessing would file one person's private chat under another's account.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

from ...db import repo
from ...db.tenancy import OWNER_TENANT_ID, TenantScope
from ...integrations import telegram as tg_integration
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
    store = TenantScope(rt.db, inbound.tenant_id)
    row = repo.chat_get(store, "tg", inbound.chat_id)
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


def _owner_tg_id(rt: Runtime, tenant_id: int) -> str | None:
    """The Telegram account behind a tenant — used to tell "sent by them" from
    "sent to them". The owner's comes from config; a product tenant's from
    their link row."""
    if tenant_id == OWNER_TENANT_ID:
        return str(rt.settings.telegram_owner_id)
    row = repo.telegram_link_get(TenantScope(rt.db, tenant_id))
    return row["tg_user_id"] if row else None


def _to_inbound(rt: Runtime, message: Message, *, is_edit: bool = False,
                tenant_id: int = OWNER_TENANT_ID) -> InboundMessage:
    sender = message.from_user
    own_id = _owner_tg_id(rt, tenant_id)
    is_from_me = bool(sender and own_id is not None and str(sender.id) == own_id)
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
        tenant_id=tenant_id,
        business_connection_id=message.business_connection_id,
        raw={"has_photo": bool(message.photo)},
    )


def _rights_dict(connection: BusinessConnection) -> dict:
    """The BusinessBotRights the user granted, as plain data.

    Older Bot API versions expose a single `can_reply` bool instead of a rights
    object; both are normalised here so the send path has one shape to check.
    """
    rights = getattr(connection, "rights", None)
    if rights is None:
        legacy = getattr(connection, "can_reply", None)
        return {} if legacy is None else {"can_reply": bool(legacy)}
    out = {}
    for field in ("can_reply", "can_read_messages", "can_delete_sent_messages",
                  "can_edit_name", "can_change_gift_settings"):
        value = getattr(rights, field, None)
        if value is not None:
            out[field] = bool(value)
    return out


def _resolve_tenant(rt: Runtime, business_connection_id: str | None) -> int | None:
    """Which tenant an inbound business update belongs to.

    Single-user (multitenant off) is always the owner — the only person who can
    have connected the bot. In product mode the connection id is the routing
    key, and an unknown one returns None so the caller drops the update instead
    of filing someone's private chat under the wrong account.
    """
    if not rt.settings.multitenant_enabled:
        return OWNER_TENANT_ID
    return tg_integration.tenant_for_connection(rt, business_connection_id)


def register(dp: Dispatcher, rt: Runtime) -> None:
    @dp.business_connection()
    async def on_business_connection(connection: BusinessConnection) -> None:
        enabled = getattr(connection, "is_enabled", None)
        if enabled is None:
            rights = getattr(connection, "rights", None)
            enabled = rights is not None

        if rt.settings.multitenant_enabled:
            user = getattr(connection, "user", None)
            tenant_id = tg_integration.record_connection(
                rt, tg_user_id=str(user.id) if user else "",
                business_connection_id=connection.id, enabled=bool(enabled),
                rights=_rights_dict(connection),
            )
            if tenant_id is None:
                # Connected by someone who never linked their account: tell them
                # how, rather than silently ignoring a bot they just installed.
                bot: Bot | None = rt.clients.get("control_bot")  # type: ignore[assignment]
                if bot and user:
                    try:
                        await bot.send_message(
                            user.id,
                            "Thanks for connecting! Finish linking in the Archon "
                            "app first — it will give you a code to send here.")
                    except Exception:  # noqa: BLE001
                        pass
                return
            rt.health["tg_business"] = "connected" if enabled else "disconnected"
            return

        # Single-user: unchanged behaviour, owner-scoped setting + owner alert.
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

        tenant_id = _resolve_tenant(rt, message.business_connection_id)
        if tenant_id is None:
            rt.audit.note("tg_business_message_unrouted")
            return

        is_from_me = bool(message.from_user
                          and message.from_user.id == rt.settings.telegram_owner_id)
        if is_from_me and message.business_connection_id:
            url = download_cmd.is_download_command(message.text)
            if url:
                await download_cmd.handle_business(
                    rt, message.bot, message.chat.id, message.message_id,
                    message.business_connection_id, url)
                return

        inbound = _to_inbound(rt, message, tenant_id=tenant_id)
        bot: Bot | None = rt.clients.get("control_bot")  # type: ignore[assignment]
        if bot:
            await _download_photo_if_wanted(rt, bot, message, inbound)
        await rt.bus.publish(inbound)

    @dp.edited_business_message()
    async def on_edited_business_message(message: Message) -> None:
        tenant_id = _resolve_tenant(rt, message.business_connection_id)
        if tenant_id is None:
            return
        await rt.bus.publish(_to_inbound(rt, message, is_edit=True,
                                         tenant_id=tenant_id))

    @dp.deleted_business_messages()
    async def on_deleted_business_messages(event: BusinessMessagesDeleted) -> None:
        tenant_id = _resolve_tenant(
            rt, getattr(event, "business_connection_id", None))
        if tenant_id is None:
            return
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
                tenant_id=tenant_id,
            ))
