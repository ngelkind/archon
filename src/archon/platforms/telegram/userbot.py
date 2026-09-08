"""Telethon userbot: the owner's own account session.

Covers what Business connections cannot: group/channel reading (whitelist),
the full dialog list (whitelist-by-approximate-name), group deletion/edit
logging, native scheduled messages, and sending as the owner in groups.

Partition rule: the userbot IGNORES private chats entirely — those arrive via
the Business connection. Belt-and-suspenders: the messages-table unique index
would drop duplicates anyway.

Structure: :func:`build_client` makes the Telethon client, :func:`wire_events`
registers the handlers on any client-shaped object, :func:`run` is the
supervised loop. The split exists so the handlers are reachable without a live
MTProto connection — they had zero test coverage while nested inside ``run``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from ...db import repo
from ...models import InboundMessage, MediaRef
from ...runtime import Runtime

HEALTH_KEY = "tg_userbot"
NOT_CONFIGURED = "disabled (not configured: TELEGRAM_API_ID/HASH/TELETHON_SESSION)"
SESSION_INVALID = "SESSION INVALID — re-run scripts/telethon_login.py"


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


# TODO(TOS-REVIEW): Telegram — detects self-destruct (TTL) media — review before launch
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


def _log_channel_id(rt: Runtime) -> str:
    """Read per event (not once at startup): ``log_channel_set`` retargets the
    channel at runtime and a stale closure would re-ingest the new one."""
    return str(repo.setting_get(rt.db, "log.channel_id", rt.settings.tg_log_channel_id) or "")


async def _sync_dialogs(rt: Runtime, client: Any) -> int:
    count = 0
    async for dialog in client.iter_dialogs(limit=500):
        # Private chats are synced too (as kind "private") so the owner's
        # contacts are findable by name for sends — the Business partition
        # governs message INGESTION dedupe, not the dialog directory.
        if dialog.is_user:
            kind = "private"
        elif dialog.is_channel and not dialog.is_group:
            kind = "channel"
        else:
            kind = "group"
        repo.chat_upsert(rt.db, "tg", _norm_chat_id(dialog.id),
                         dialog.name or None, kind)
        count += 1
    return count


def resolve_peerless_delete(rt: Runtime, msg_id: str) -> str | None:
    """Which chat a ``MessageDeleted`` without a peer belongs to.

    Telethon delivers ``UpdateDeleteMessages`` (private chats and legacy small
    groups) with ``peer=None``; supergroups and channels always carry theirs.
    The cache is the only way to attribute such a delete, and the old lookup —
    any tg row with that message id — stamped ``deleted_at`` on whichever chat
    happened to reuse the number (Telegram message ids are per-chat in
    supergroups). Attribution is therefore taken only when it is unambiguous:
    exactly one cached row, written by this userbot (private chats are the
    Business connection's, which emits its own delete update), and not in a
    ``-100`` chat (those never arrive peerless).
    """
    rows = repo.message_search(
        rt.db,
        "platform = 'tg' AND msg_id = ? AND source = 'userbot' "
        "AND chat_id NOT LIKE '-100%'",
        (msg_id,),
    )
    if len(rows) != 1:
        return None
    return str(rows[0]["chat_id"])


def build_client(rt: Runtime) -> TelegramClient | None:
    s = rt.settings
    if not (s.telegram_api_id and s.telegram_api_hash and s.telethon_session):
        return None
    return TelegramClient(
        StringSession(s.telethon_session), s.telegram_api_id, s.telegram_api_hash
    )


def wire_events(rt: Runtime, client: Any) -> None:
    """Register the three inbound handlers on ``client``.

    ``client`` only needs ``on(builder)`` plus the Telethon message/event
    attribute surface the handlers read, so a fake can stand in for tests.
    """

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

    # TODO(TOS-REVIEW): Telegram — captures and saves self-destruct (TTL) media the sender intended to vanish — review before launch
    async def _capture_ephemeral(event: Any, chat_kind: str) -> None:
        from ...logging_ import capture

        chat_id = _norm_chat_id(event.chat_id)
        kind = _ephemeral_kind(event.message)
        if not capture.capture_enabled(rt, "tg", chat_id, chat_kind):
            rt.audit.note("tg_capture_disarmed", chat=chat_id, kind=kind or "document")
            return
        try:
            rt.settings.media_dir.mkdir(parents=True, exist_ok=True)
            path = rt.settings.media_dir / f"tg-vo-{chat_id}-{event.message.id}"
            saved = await event.message.download_media(file=str(path))
            if not saved:
                rt.audit.note("tg_capture_empty", chat=chat_id,
                              msg_id=str(event.message.id), kind=kind or "document")
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
        # The bot posts to the log channel; ignore it so we don't re-ingest.
        log_channel_id = _log_channel_id(rt)
        if log_channel_id and _norm_chat_id(event.chat_id) == log_channel_id:
            return
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
        if getattr(event.message, "photo", None):
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
        # Supergroups/channels carry chat_id; private chats and legacy groups
        # arrive with no peer at all (event.chat_id is None — and so is
        # event.is_private, which is why the old `if event.is_private` guard
        # could never fire). Resolve from the cache only when unambiguous.
        chat_id = _norm_chat_id(event.chat_id) if event.chat_id else None
        for msg_id in event.deleted_ids:
            resolved_chat = chat_id
            if resolved_chat is None:
                resolved_chat = resolve_peerless_delete(rt, str(msg_id))
            if resolved_chat is None:
                rt.audit.note("tg_delete_unresolved", msg_id=str(msg_id))
                continue
            await rt.bus.publish(InboundMessage(
                platform="tg", source="userbot", chat_id=resolved_chat,
                chat_kind="group", msg_id=str(msg_id), sender_id="unknown",
                ts=datetime.now(UTC), is_delete=True,
            ))


async def run(rt: Runtime, client: Any = None) -> None:
    """Supervised loop. ``client`` is injectable for tests; production builds
    the real Telethon client from settings.

    Failure semantics: an unconfigured userbot returns cleanly (a deliberate
    disabled state); an invalid session or a lost connection RAISES so the
    supervisor backs off, retries and alerts — a clean return here used to mean
    "dead for the rest of the process with only a health string as evidence".
    ``rt.clients["tg_userbot"]`` is present only while the client is usable:
    it is set after authorisation succeeds and removed on the way out, so the
    send tools' Business fallback can engage instead of hitting a dead client.
    """
    if client is None:
        client = build_client(rt)
    if client is None:
        rt.health[HEALTH_KEY] = NOT_CONFIGURED
        return

    wire_events(rt, client)
    await client.connect()
    if not await client.is_user_authorized():
        rt.health[HEALTH_KEY] = SESSION_INVALID
        rt.audit.note("tg_userbot_unauthorized")
        raise RuntimeError("telethon session is not authorised")

    me = await client.get_me()
    rt.audit.note("tg_userbot_started", user=me.username or me.id)
    synced = await _sync_dialogs(rt, client)
    rt.audit.note("tg_dialogs_synced", count=synced)
    rt.clients["tg_userbot"] = client
    rt.health[HEALTH_KEY] = "connected"
    try:
        await client.run_until_disconnected()
        # Telethon gave up reconnecting: that is a failure, not a shutdown.
        rt.audit.note("tg_userbot_disconnected")
        raise RuntimeError("telethon disconnected")
    finally:
        rt.clients.pop("tg_userbot", None)
        rt.health[HEALTH_KEY] = "disconnected"


# --- helpers used by tools ----------------------------------------------------

async def _resolve_ref(client: TelegramClient, ref: str) -> Any:
    """Resolve a Telegram peer from a numeric id, @username, or phone number.

    A phone that isn't already a contact is imported first (Telethon can only
    get_entity a phone that the account knows). This is how "message dad at
    +972..." works without an existing chat."""
    ref = str(ref).strip()
    # Numeric chat id (may be negative for groups) -> resolve directly.
    try:
        return await client.get_entity(int(ref))
    except (ValueError, TypeError):
        pass
    except Exception:
        pass  # fall through to string resolution
    try:
        return await client.get_entity(ref)  # @username, t.me link, or known phone
    except Exception:
        digits = ref.lstrip("+")
        if digits.isdigit():
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact

            res = await client(ImportContactsRequest(
                [InputPhoneContact(client_id=0, phone="+" + digits,
                                   first_name="Contact", last_name="")]))
            if getattr(res, "users", None):
                return res.users[0]
            raise RuntimeError(f"phone {ref} is not on Telegram (or hides its number)")
        raise


async def send_as_owner(rt: Runtime, chat_id: str, text: str,
                        schedule: datetime | None = None,
                        reply_to: str | int | None = None) -> str:
    client: TelegramClient | None = rt.clients.get("tg_userbot")  # type: ignore[assignment]
    if client is None:
        raise RuntimeError("Telegram userbot is not connected")
    entity = await _resolve_ref(client, chat_id)
    kwargs: dict[str, Any] = {}
    if reply_to:
        try:
            kwargs["reply_to"] = int(reply_to)  # quote/tag the message we answer
        except (TypeError, ValueError):
            pass
    msg = await client.send_message(entity, text, schedule=schedule, **kwargs)
    return str(getattr(msg, "id", "sent"))


async def refresh_dialogs(rt: Runtime) -> int:
    client: TelegramClient | None = rt.clients.get("tg_userbot")  # type: ignore[assignment]
    if client is None:
        raise RuntimeError("Telegram userbot is not connected")
    return await _sync_dialogs(rt, client)
